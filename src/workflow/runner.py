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
import importlib
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from src.config import AppConfig, ParliamentConfig
from src.services.job_manager import Job, JobManager
from src.services.log_streamer import LogStreamer
from src.services.notifier import JobResult, SlackNotifier
from src.workflow.progress import _SESSION_PATTERNS, _STAGE_PATTERNS

logger = logging.getLogger(__name__)


# UI stages that drive `execute_workflow` flags — `publish` is handled separately.
_PIPELINE_STAGES = {"download", "parse", "merge", "nel", "align", "ner"}

# Grace period after SIGTERM before we SIGKILL on cancellation.
_CANCEL_GRACE_SECONDS = 10


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

        await self.log_streamer.append(job.id, f"=== Job {job.id} started: {job.stages}")
        pipeline_stages = [s for s in job.stages if s in _PIPELINE_STAGES]
        publish_requested = "publish" in job.stages or (job.publish_on_success and pipeline_stages)

        try:
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
        """Estimate how many sessions the progress counter will actually count.

        The denominator must measure the same thing the live numerator does.
        `sessions_completed` is bumped only by per-session log lines for
        nel/align/ner (see `progress._SESSION_PATTERNS`); download/parse/merge
        emit no per-session signal. So we count only the nel/align/ner-needing
        sessions among the requested stages. Counting merge-needing sessions too
        (as this used to) inflates the total — e.g. an update job where 85
        sessions need re-merging but only 3 need nel/align showed "0/85 … 3/85",
        with the bar stuck because the counter could never reach 85. Now it reads
        "0/3 … 3/3". Gates mirror workflow.py: nel → `SessionStatus.linked`
        absent; align → `aligned` absent or merged newer; ner → `ner` absent.
        `force=True` bypasses the gates and counts every in-scope session (they
        all get re-run through the requested progress stages).

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
            return len(sessions)

        todo = 0
        for s in sessions:
            try:
                status = cfg.status(s)
            except Exception:
                # If status check fails (corrupt JSON, missing dir, etc.) assume
                # this session needs attention rather than silently dropping it.
                todo += 1
                continue
            if "nel" in progress_stages and SessionStatus.linked not in status:
                todo += 1
                continue
            if "align" in progress_stages and (
                SessionStatus.aligned not in status or cfg.is_newer(s, "merged", "aligned")
            ):
                todo += 1
                continue
            if "ner" in progress_stages and SessionStatus.ner not in status:
                todo += 1
                continue
        return todo

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
            await self.log_streamer.append(job.id, f"$ {' '.join(cmd)}  (cwd={repo_dir} = {label})")
            try:
                proc = await asyncio.create_subprocess_exec(
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

        cmd = [sys.executable, "-u", "-m", f"optv.parliaments.{job.parliament}.workflow", *argv]

        job.stage = stages[0]
        self.job_manager.set_current(job)
        await self.log_streamer.append(job.id, f"$ {' '.join(cmd)}  (cwd={tools_dir})")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(tools_dir),
            env=env,
            start_new_session=True,  # own process group, so cancel can kill ffmpeg/aeneas too
        )

        watcher = asyncio.create_task(self._watch_cancellation(job, proc))
        seen_sessions: set[str] = set()
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

    def _apply_log_patterns(self, job: Job, line: str, seen_sessions: set[str]) -> None:
        """Update job.stage and job.sessions_completed from a subprocess log line.

        Reuses `_STAGE_PATTERNS` and `_SESSION_PATTERNS` from `progress.py` —
        they `pat.search()` for substrings, so they match formatted log lines
        (with timestamp/level prefixes) just as well as raw record messages.
        """
        changed = False
        for pat, stage in _STAGE_PATTERNS:
            if pat.search(line) and job.stage != stage:
                job.stage = stage
                changed = True
                break

        for pat in _SESSION_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            session = m.group(1)
            if session in seen_sessions:
                break
            seen_sessions.add(session)
            job.current_session = session
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
            await self.log_streamer.append(job.id, f"$ {' '.join(cmd)}  (cwd={data_dir})")
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
                raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}")
