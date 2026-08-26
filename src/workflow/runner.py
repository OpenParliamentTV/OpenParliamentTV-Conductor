"""Workflow runner — spawns Tools' parliament-specific workflow as a subprocess.

Strategy:
  1. Auto-pull `parliament.tools_dir` and `parliament.data_dir` so the subprocess
     runs against the latest Tools code (matches legacy `optv pull` semantics).
  2. Spawn `python -m optv.parliaments.<id>.workflow ...` with PYTHONPATH set
     so each job picks up Tools changes without restarting Conductor.
  3. Stream subprocess stdout to LogStreamer and pattern-match for stage/session
     progress (regexes in `progress.py`).
  4. For the optional `publish` stage, run `git add/commit/push` in the data_dir
     using `GIT_USER_NAME`/`GIT_USER_EMAIL` from settings (no global git config).

Host resource exhaustion: when the box runs out of process slots, every spawn
fails with EAGAIN — git can't fork, and the workflow dies importing numpy
because OpenBLAS can't start its threads. That is not a pipeline fault and
retrying cannot fix it, so `run_job` checks the pids cgroup before doing
anything (a file read, not a fork — the check has to work on a host that is
already out of slots) and raises `HostResourceError` instead of running a
doomed job. Where the cgroup is unreadable or unlimited, `_spawn` classifies
EAGAIN/ENOMEM at spawn time into the same error. A schedule that keeps failing
is paused automatically; see `_maybe_pause_schedule`.

Cancellation: a background task watches the cancellation flag and terminates
the subprocess on signal (SIGTERM, then SIGKILL after a 10s grace period). The
workflow runs in its own process group (`start_new_session=True`) and the signal
is sent to the whole group via `os.killpg`, not just the direct child — during
alignment the workflow forks ffmpeg and an aeneas multiprocessing worker that
inherit the stdout pipe, so killing only the parent would orphan them, leaving
the pipe's write end open and the `async for proc.stdout` read loop blocked
forever (the job would then never leave the `cancelling` state).
"""

from __future__ import annotations

import asyncio
import errno
import importlib
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from src import __version__
from src.config import AppConfig, ParliamentConfig, save_schedules
from src.services.job_manager import Job, JobManager
from src.services.log_streamer import LogStreamer
from src.services.notifier import JobResult, SlackNotifier
from src.workflow.progress import _SESSION_PATTERNS, _STAGE_PATTERNS

logger = logging.getLogger(__name__)


# UI stages that drive `execute_workflow` flags — `publish` is handled separately.
_PIPELINE_STAGES = {"download", "parse", "merge", "nel", "align", "ner"}

# Grace period after SIGTERM before we SIGKILL on cancellation.
_CANCEL_GRACE_SECONDS = 10

# Consecutive failed runs after which a schedule pauses itself. At the DE
# cadence (every 5 minutes) this is ~50 minutes of retrying before giving up —
# long enough to ride out a transient outage, short enough that a genuinely
# broken host produces ten failures and a notification rather than thousands.
_MAX_CONSECUTIVE_FAILURES = 10

# pids cgroup, v2 layout first then v1. Read, never forked — the whole point is
# to answer "can we still start processes?" on a host where we can't.
_PIDS_CURRENT_PATHS = ("/sys/fs/cgroup/pids.current", "/sys/fs/cgroup/pids/pids.current")
_PIDS_MAX_PATHS = ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max")

# Refuse to start a pipeline with less than this many pids left. An align job
# runs parent + spawned aeneas child + ffmpeg, each with its own thread pools,
# so starting with a near-empty budget just fails further in and more noisily.
_PIDS_MIN_HEADROOM = 64


class HostResourceError(RuntimeError):
    """The host cannot start processes — no point running (or retrying) a job."""


def _read_first_line(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _pids_usage() -> tuple[int, int] | None:
    """Return (current, max) for this container's pids cgroup.

    None when the controller is absent or unlimited (`max`), which is also the
    normal case outside a container — callers then just skip the preflight and
    rely on spawn-time EAGAIN classification instead.
    """
    current = _read_first_line(_PIDS_CURRENT_PATHS)
    maximum = _read_first_line(_PIDS_MAX_PATHS)
    if current is None or maximum is None or maximum == "max":
        return None
    try:
        return int(current), int(maximum)
    except ValueError:
        return None


class WorkflowRunner:
    def __init__(
        self,
        config: AppConfig,
        job_manager: JobManager,
        log_streamer: LogStreamer,
        notifier: SlackNotifier | None = None,
    ) -> None:
        self.config = config
        self.job_manager = job_manager
        self.log_streamer = log_streamer
        self.notifier = notifier

    async def run_job(self, job: Job) -> Job:
        parliament = self.config.parliaments[job.parliament]
        started = time.time()

        # Version in the job log: log excerpts get pasted around without any
        # note of which build produced them, and "is the fix deployed?" is the
        # first question asked of a failing job.
        await self.log_streamer.append(
            job.id, f"=== Job {job.id} started: {job.stages} (conductor {__version__})"
        )
        pipeline_stages = [s for s in job.stages if s in _PIPELINE_STAGES]
        publish_requested = "publish" in job.stages or (job.publish_on_success and pipeline_stages)

        try:
            await self._check_process_headroom(job)
            if pipeline_stages:
                # Iterates every session calling Tools' status checks — can take
                # seconds on a large data dir. Run it off the event loop so the
                # UI stays responsive while a job is starting up.
                job.sessions_total = await asyncio.to_thread(
                    self._estimate_total_sessions, job, parliament
                )
                self.job_manager.set_current(job)
                await self._run_pipeline(job, parliament, pipeline_stages)

            if self._is_cancelled(job):
                job.status = "cancelled"
            else:
                if publish_requested:
                    job.stage = "publish"
                    self.job_manager.set_current(job)
                    await self._run_publish(job, parliament)
                job.status = "completed" if not job.failed_sessions else "partial"
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            await self.log_streamer.append(job.id, f"!!! Job failed: {job.error}")
            logger.exception("Job %s failed", job.id)

        job.progress = 100
        self.job_manager.complete(job)
        await self.log_streamer.append(job.id, f"=== Job {job.id} finished: {job.status}")

        # After complete(), so this run is part of the history it counts.
        await self._maybe_pause_schedule(job)

        if self.notifier:
            duration = int(time.time() - started)
            await self.notifier.notify(
                JobResult(
                    job_id=job.id,
                    job_name=job.schedule_id or f"{job.parliament} manual",
                    parliament=job.parliament,
                    success=(job.status == "completed"),
                    partial=(job.status == "partial"),
                    stage=job.stage if job.status in {"failed", "partial"} else None,
                    duration_seconds=duration,
                    sessions_total=job.sessions_total,
                    sessions_completed=job.sessions_completed,
                    failed_sessions=job.failed_sessions,
                ),
                source=job.source,
            )
        return job

    def _is_cancelled(self, job: Job) -> bool:
        current = self.job_manager.current()
        if current and current.id == job.id and current.status == "cancelling":
            return True
        return False

    def _estimate_total_sessions(self, job: Job, parliament: ParliamentConfig) -> int:
        """Estimate how many work-units the progress counter will actually count.

        The denominator must measure the same thing the live numerator does.
        `sessions_completed` counts per `(stage, session)` work-units across the
        nel/align/ner stages (see `progress._SESSION_PATTERNS` and
        `_apply_log_patterns`); download/parse/merge emit no per-session signal.
        So a session that needs both nel and align counts as TWO units here, not
        one — otherwise a 235-session nel+align run would show a denominator of
        235 while the numerator climbs to 470, and the bar would hit 100% halfway
        through nel and never move during align. Counting merge-needing sessions
        too (as this once did) would also inflate the total — an update job where
        85 sessions need re-merging but only 3 need nel showed "0/85 … 3/85" with
        the bar stuck, because the counter could never reach 85.

        Gates mirror workflow.py: nel → `SessionStatus.linked` absent; align →
        not `no_text` and `aligned` absent and merged newer (an `or` here counted
        every already-aligned session whenever the merge stage had just bumped
        the merged cache's mtime); ner → `ner` absent. `force=True` bypasses the
        gates: every in-scope session is re-run through every requested progress
        stage, so the total is `sessions × stages`.

        Returns 0 ("unknown") when there's no per-session signal to count against
        (download/parse/merge-only jobs) or we can't predict — the template then
        shows a bare counter with no denominator.

        Note: this is the only in-process Tools import remaining. `common.py` is
        stable; restart Conductor to pick up changes there.
        """
        progress_stages = {s for s in job.stages if s in {"nel", "align", "ner"}}
        if not progress_stages:
            return 0  # no per-session progress signal — show a bare counter

        module = importlib.import_module(f"optv.parliaments.{job.parliament}.common")
        cfg = module.Config(Path(parliament.data_dir))
        SessionStatus = module.SessionStatus

        sessions = cfg.sessions()
        if job.session_filter:
            # A session_filter pins exact sessions; the period prefix filter
            # (a DE-only ID convention) is redundant and would wrongly drop
            # sessions whose ID doesn't start with `job.period`. Mirror the
            # `--no-limit-to-period` choice in `_build_argv`.
            pattern = re.compile(job.session_filter)
            sessions = [s for s in sessions if pattern.match(s)]
        elif job.period:
            sessions = [s for s in sessions if s.startswith(str(job.period))]

        if job.force:
            return len(sessions) * len(progress_stages)

        todo = 0
        for s in sessions:
            try:
                status = cfg.status(s)
            except Exception:
                # If status check fails (corrupt JSON, missing dir, etc.) assume
                # this session needs every requested stage rather than silently
                # dropping it.
                todo += len(progress_stages)
                continue
            if "nel" in progress_stages and SessionStatus.linked not in status:
                todo += 1
            if "align" in progress_stages and (
                SessionStatus.no_text not in status
                and SessionStatus.aligned not in status
                and cfg.is_newer(s, "merged", "aligned")
            ):
                todo += 1
            if "ner" in progress_stages and SessionStatus.ner not in status:
                todo += 1
        return todo

    @staticmethod
    async def _spawn(*cmd: str, **kwargs: Any) -> asyncio.subprocess.Process:
        """`create_subprocess_exec` that names host exhaustion for what it is.

        EAGAIN/ENOMEM from fork, and RuntimeError from asyncio's per-subprocess
        reaper thread, both mean the same thing: the host has no room to start
        a process. Unwrapped they surface as a bare
        `BlockingIOError: [Errno 11] Resource temporarily unavailable` with no
        indication of which command or why — and, from `_pull_repos`, as a job
        that dies on a best-effort git pull. Every other OSError (a missing
        binary, a bad cwd) is re-raised untouched so callers can still handle
        it themselves.
        """
        try:
            return await asyncio.create_subprocess_exec(*cmd, **kwargs)
        except (OSError, RuntimeError) as exc:
            # BlockingIOError is Python's own mapping of EAGAIN/EWOULDBLOCK —
            # matched by type rather than by number, since errno.EAGAIN is 11 on
            # Linux but 35 on macOS. ENOMEM is the out-of-memory variant, and
            # RuntimeError is asyncio failing to start the reaper thread it
            # needs per subprocess.
            exhausted = isinstance(exc, (BlockingIOError, RuntimeError)) or (
                isinstance(exc, OSError) and exc.errno == errno.ENOMEM
            )
            if not exhausted:
                raise
            raise HostResourceError(
                f"host cannot start new processes — `{shlex.join(cmd)}` failed with {exc}"
            ) from exc

    async def _check_process_headroom(self, job: Job) -> None:
        """Abort before spawning if the host has no process slots left.

        Without this the job runs anyway and fails deep inside the workflow's
        numpy import, with an OpenBLAS thread-creation error and a traceback
        that looks like a pipeline bug. The cause is the host, so say so, in
        one line, before doing any work.
        """
        usage = _pids_usage()
        if usage is None:
            return
        current, maximum = usage
        if maximum - current >= _PIDS_MIN_HEADROOM:
            return
        raise HostResourceError(
            f"host is out of process slots (pids {current}/{maximum}) — not starting "
            f"the workflow. Free processes on the host or raise the container's "
            f"pids limit, then re-run."
        )

    async def _maybe_pause_schedule(self, job: Job) -> None:
        """Pause a schedule that has failed `_MAX_CONSECUTIVE_FAILURES` times.

        A cron firing every few minutes against a broken host retries forever:
        it cannot fix itself, each run writes another job log, and the noise
        buries the first failure — the only one that explains anything. Pausing
        is reversible from the UI and is persisted the same way the UI persists
        it, so the pause survives a restart or rebuild.
        """
        if job.status != "failed" or not job.schedule_id:
            return
        failures = self.job_manager.consecutive_failures(job.schedule_id)
        if failures < _MAX_CONSECUTIVE_FAILURES:
            return
        sched = self.config.schedules.get(job.schedule_id)
        if sched is None or not sched.enabled:
            return

        sched.enabled = False
        try:
            save_schedules(self.config)
        except OSError as exc:
            # Read-only config mount: roll back so the UI doesn't show a pause
            # that isn't real. The storm continues, but visibly.
            sched.enabled = True
            logger.error("Could not pause schedule %s: %s", job.schedule_id, exc)
            await self.log_streamer.append(
                job.id, f"!! Could not pause schedule '{job.schedule_id}': {exc}"
            )
            return

        message = (
            f"Schedule '{job.schedule_id}' paused automatically after {failures} "
            f"consecutive failures. Last error: {job.error or 'unknown'}. "
            f"Fix the cause, then re-enable it from the schedules page."
        )
        logger.error(message)
        await self.log_streamer.append(job.id, f"!!! {message}")
        if self.notifier:
            await self.notifier.notify_text(f":rotating_light: {message}")

    async def _pull_repos(self, job: Job, parliament: ParliamentConfig) -> None:
        """Git-pull Tools and Data before running the pipeline.

        Matches legacy `optv pull` semantics: best-effort, log+continue on failure.
        Subprocess executions of the workflow always start from the freshly-pulled
        Tools code on disk, so this is what makes Tools changes auto-propagate.

        The other half of legacy `optv pull` — refreshing the NEL entity dump —
        is requested via `--update-nel-entities` in `_build_argv` (it runs in
        the workflow subprocess, against the data dir), not here.
        """
        for label, repo_dir in (("Tools", parliament.tools_dir), ("Data", parliament.data_dir)):
            cmd = ["git", "pull", "--ff-only"]
            await self.log_streamer.append(job.id, f"$ {shlex.join(cmd)}  (cwd={repo_dir} = {label})")
            try:
                proc = await self._spawn(
                    *cmd,
                    cwd=str(repo_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except FileNotFoundError as exc:
                await self.log_streamer.append(job.id, f"!! git not found or {repo_dir} missing: {exc} — continuing")
                continue
            assert proc.stdout is not None
            async for line_bytes in proc.stdout:
                await self.log_streamer.append(job.id, line_bytes.decode("utf-8", errors="replace").rstrip())
            rc = await proc.wait()
            if rc != 0:
                await self.log_streamer.append(
                    job.id, f"!! git pull {label} exited {rc} — continuing with on-disk version"
                )

    async def _run_pipeline(
        self,
        job: Job,
        parliament: ParliamentConfig,
        stages: list[str],
    ) -> None:
        await self._pull_repos(job, parliament)

        argv = self._build_argv(job, parliament, stages)
        tools_dir = Path(parliament.tools_dir)

        env = {**os.environ}
        env["PYTHONPATH"] = str(tools_dir) + os.pathsep + env.get("PYTHONPATH", "")
        # Cap the BLAS pools. numpy/OpenBLAS starts one thread per core in every
        # process that imports it, and alignment imports it twice over (the
        # workflow parent plus a freshly spawned aeneas child per speech). None
        # of that work is BLAS-bound and the pipeline is serial per speech, so
        # the pools buy nothing and cost a 4x thread multiplier on a 4-core Pi.
        # setdefault, so an operator can still override from secrets.env.
        for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            env.setdefault(var, "1")

        cmd = [sys.executable, "-u", "-m", f"optv.parliaments.{job.parliament}.workflow", *argv]

        job.stage = stages[0]
        self.job_manager.set_current(job)
        await self.log_streamer.append(job.id, f"$ {shlex.join(cmd)}  (cwd={tools_dir})")

        proc = await self._spawn(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(tools_dir),
            env=env,
            start_new_session=True,  # own process group, so cancel can kill ffmpeg/aeneas too
        )

        watcher = asyncio.create_task(self._watch_cancellation(job, proc))
        seen_sessions: set[tuple[str, str]] = set()
        try:
            assert proc.stdout is not None
            async for line_bytes in proc.stdout:
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                await self.log_streamer.append(job.id, line)
                self._apply_log_patterns(job, line, seen_sessions)
        finally:
            watcher.cancel()

        rc = await proc.wait()

        if self._is_cancelled(job):
            return  # caller sets status to "cancelled"
        if rc != 0:
            raise RuntimeError(f"Workflow exited with code {rc}")

    async def _watch_cancellation(self, job: Job, proc: asyncio.subprocess.Process) -> None:
        """Poll the cancellation flag and signal the subprocess if set.

        Sleep 1s between checks — gives ~1s cancellation latency regardless of
        how chatty the subprocess is. Without this, cancellation only kicks in
        when a new line arrives on stdout (could be minutes during alignment).

        Signals the whole process group (SIGTERM, then SIGKILL after the grace
        period), not just `proc`: the workflow forks ffmpeg + an aeneas worker
        during alignment, and they must die too so the stdout pipe reaches EOF
        and the reader loop in `_run_workflow` unblocks. See module docstring.
        """
        try:
            while proc.returncode is None:
                if self._is_cancelled(job):
                    self._signal_group(proc, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=_CANCEL_GRACE_SECONDS)
                    except asyncio.TimeoutError:
                        self._signal_group(proc, signal.SIGKILL)
                    return
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Send `sig` to the subprocess's whole process group, falling back to
        the lone process if the group is already gone."""
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            return  # already exited
        except OSError:
            # No process group (shouldn't happen with start_new_session) — hit
            # the direct child so cancellation still makes progress.
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def _apply_log_patterns(
        self, job: Job, line: str, seen_sessions: set[tuple[str, str]]
    ) -> None:
        """Update job.stage and job.sessions_completed from a subprocess log line.

        Reuses `_STAGE_PATTERNS` and `_SESSION_PATTERNS` from `progress.py` —
        they `pat.search()` for substrings, so they match formatted log lines
        (with timestamp/level prefixes) just as well as raw record messages.

        Progress is counted per `(stage, session)` work-unit, not per bare
        session id: a job running nel+align+ner processes every session three
        times, so deduping on the id alone let the nel pass fill the counter to
        100% and then froze align/ner at "N/N" with `current_session` stuck on
        the last-linked session (the stage label still advanced — that's a
        separate, un-deduped pattern). Each `_SESSION_PATTERNS` entry carries its
        stage; `None` means "use whatever stage is live now" (the publish
        sub-step, see progress.py).
        """
        changed = False
        for pat, stage in _STAGE_PATTERNS:
            if pat.search(line) and job.stage != stage:
                job.stage = stage
                changed = True
                break

        for pat, stage in _SESSION_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            session = m.group(1)
            # current_session always tracks the latest per-session line so the
            # UI spinner follows the session being worked now, even within a
            # stage we have already counted this session for.
            if job.current_session != session:
                job.current_session = session
                changed = True
            key = (stage or job.stage, session)
            if key in seen_sessions:
                break
            seen_sessions.add(key)
            job.sessions_completed = len(seen_sessions)
            # Keep the denominator >= the numerator. The estimate can undercount
            # (drift, or the publish step announcing sessions beyond the nel/align
            # set), and "4/3 sessions" reads as broken — grow the total instead so
            # it stays consistent and still ends at "N/N".
            if job.sessions_completed > job.sessions_total:
                job.sessions_total = job.sessions_completed
            if job.sessions_total:
                job.progress = min(
                    100,
                    int(100 * job.sessions_completed / job.sessions_total),
                )
            changed = True
            break

        if changed:
            self.job_manager.set_current(job)

    def _build_argv(self, job: Job, parliament: ParliamentConfig, stages: list[str]) -> list[str]:
        """Build the CLI argument list for `python -m optv.parliaments.<id>.workflow`."""
        stage_set = set(stages)
        cache_dir = (
            Path(parliament.cache_dir) if parliament.cache_dir
            else Path(parliament.data_dir) / "cache"
        )
        argv = [
            str(parliament.data_dir),
            f"--period={job.period or parliament.current_period}",
            f"--retry-count={parliament.retry_count}",
            f"--cache-dir={cache_dir}",
            f"--lang={parliament.language}",
            f"--limit-session={job.session_filter or ''}",
            f"--ner-api-endpoint={self.config.settings.ner_api_endpoint}",
            "--align-timeout=1200",
            "--align-max-audio-seconds=2400",
            "--no-single-instance",
        ]
        # `--limit-to-period` is a prefix filter (`session.startswith(period)`),
        # a DE-only ID convention. When `session_filter` already pins exact
        # sessions, that prefix filter is redundant and actively harmful if the
        # job's period is wrong — so disable it for targeted reruns and let the
        # parliament-agnostic session-filter regex be the sole selector.
        argv.append("--no-limit-to-period" if job.session_filter else "--limit-to-period")
        if job.force:
            argv.append("--force")
        if job.rebuild:
            # Implies --force in Tools; sending both is harmless and explicit.
            argv.append("--rebuild")
        if "download" in stage_set:
            argv.append("--download-original")
        if "merge" in stage_set:
            argv.append("--merge-speeches")
        if "align" in stage_set:
            argv.append("--align-sentences")
        # Refresh the entity registry on every job, independent of the `nel`
        # stage. `--update-nel-entities` is its own stage in Tools' workflow.py
        # (decoupled from `--link-entities`), so it runs even for download/parse/
        # align-only jobs. This restores legacy `optv pull` semantics, where the
        # cron re-fetched the entity dump unconditionally so a platform-side
        # registry change propagates without waiting for a nel job. workflow.py
        # downloads the dump (from `--nel-entity-url`, else the Tools manifest
        # `entity_dump_url`) into metadata/entities.json; the publish step commits
        # it. No-ops with a warning if no entity URL is configured.
        argv.append("--update-nel-entities")
        if parliament.entity_dump_url:
            argv.append(f"--nel-entity-url={parliament.entity_dump_url}")
        if "nel" in stage_set:
            argv.append("--link-entities")
        if "ner" in stage_set:
            argv.append("--extract-entities")
        return argv

    async def _run_publish(self, job: Job, parliament: ParliamentConfig) -> None:
        data_dir = Path(parliament.data_dir)
        message = f"optv publication on {time.strftime('%Y-%m-%dT%H:%M:%S%z')}"
        git_id_args = [
            "-c", f"user.name={self.config.settings.git_user_name}",
            "-c", f"user.email={self.config.settings.git_user_email}",
        ]
        cmds = [
            ["git", "add", "original/media", "original/proceedings", "processed", "metadata"],
            ["git", *git_id_args, "commit", "-m", message],
            ["git", "push"],
        ]
        loop = asyncio.get_running_loop()
        for cmd in cmds:
            await self.log_streamer.append(job.id, f"$ {shlex.join(cmd)}  (cwd={data_dir})")
            result = await loop.run_in_executor(
                None,
                lambda c=cmd: subprocess.run(c, cwd=data_dir, capture_output=True, text=True),
            )
            if result.stdout:
                await self.log_streamer.append(job.id, result.stdout.rstrip())
            if result.stderr:
                await self.log_streamer.append(job.id, result.stderr.rstrip())
            # `git commit` returns 1 when nothing to commit — tolerate and continue.
            if result.returncode != 0 and "commit" not in cmd:
                raise RuntimeError(f"Command failed ({result.returncode}): {shlex.join(cmd)}")
