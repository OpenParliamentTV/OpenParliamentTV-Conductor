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
the subprocess on signal (SIGTERM, then SIGKILL after a 10s grace period).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
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
                job.sessions_total = self._estimate_total_sessions(job, parliament)
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
        """Estimate how many sessions actually need work for the requested stages.

        Mimics workflow.py's per-stage gates: merge → `is_newer` on media/proceedings;
        nel/align/ner → `SessionStatus.{linked,aligned,ner}` absent. `force=True`
        bypasses the gates and counts every in-scope session.

        Returns 0 ("unknown") when we genuinely can't predict — pure-download jobs
        discover new sessions upstream, and jobs where Tools' status check raises.
        The template treats 0 as "no denominator, show a counter only".

        Note: this is the only in-process Tools import remaining. `common.py` is
        stable; restart Conductor to pick up changes there.
        """
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

        per_session_stages = {s for s in job.stages if s in {"merge", "nel", "align", "ner"}}
        if not per_session_stages:
            return 0  # download-only / publish-only — no per-session prediction

        todo = 0
        for s in sessions:
            try:
                status = cfg.status(s)
            except Exception:
                # If status check fails (corrupt JSON, missing dir, etc.) assume
                # this session needs attention rather than silently dropping it.
                todo += 1
                continue
            if "merge" in per_session_stages and (
                cfg.is_newer(s, "media", "merged") or cfg.is_newer(s, "proceedings", "merged")
            ):
                todo += 1
                continue
            if "nel" in per_session_stages and SessionStatus.linked not in status:
                todo += 1
                continue
            if "align" in per_session_stages and (
                SessionStatus.aligned not in status or cfg.is_newer(s, "merged", "aligned")
            ):
                todo += 1
                continue
            if "ner" in per_session_stages and SessionStatus.ner not in status:
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
        """
        try:
            while proc.returncode is None:
                if self._is_cancelled(job):
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=_CANCEL_GRACE_SECONDS)
                    except asyncio.TimeoutError:
                        proc.kill()
                    return
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

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
        if "nel" in stage_set:
            argv.append("--link-entities")
            # Refresh the entity registry before linking, so a platform-side
            # registry change propagates without manual intervention. Legacy
            # `optv pull` did this via curl; the Conductor must request it
            # explicitly. `workflow.py` downloads the dump (from `--nel-entity-url`
            # below, else the Tools manifest `entity_dump_url`) into
            # metadata/entities.json, and the publish step commits it.
            argv.append("--update-nel-entities")
            if parliament.entity_dump_url:
                argv.append(f"--nel-entity-url={parliament.entity_dump_url}")
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
