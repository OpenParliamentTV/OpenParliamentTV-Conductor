"""Smoke test: runner spawns the parliament's `workflow` subprocess, streams
its stdout into the job log, and bumps progress on each session message."""

from __future__ import annotations

import asyncio
import enum
import sys
import textwrap
import types
from pathlib import Path

import pytest
import yaml

from src.config import (
    AppConfig,
    ParliamentConfig,
    ParliamentStages,
    ScheduleConfig,
    Settings,
    SlackConfig,
)
from src.services.job_manager import Job, JobManager
from src.services.log_streamer import LogStreamer
from src.workflow.runner import WorkflowRunner


def _install_fake_common_in_sys_modules() -> None:
    """Inject `optv.parliaments.XX.common` so the runner's in-process
    `_estimate_total_sessions` can find a Config + SessionStatus.

    Subprocess execution (workflow.py) doesn't go through sys.modules — it
    runs `python -m` with PYTHONPATH and finds the on-disk file directly.
    """
    common = types.ModuleType("optv.parliaments.XX.common")

    class FakeSessionStatus(enum.Enum):
        media = "media"
        proceedings = "proceedings"
        merged = "merged"
        session = "session"
        linked = "linked"
        aligned = "aligned"
        ner = "ner"
        empty = "empty"
        no_text = "no_text"

    class FakeConfig:
        def __init__(self, data_dir):
            self.data_dir = Path(data_dir)
        def sessions(self, prefix=""):
            return ["21001", "21002", "21003"]
        def is_newer(self, session, stage, than):
            return True
        def status(self, session):
            return set()

    common.Config = FakeConfig
    common.SessionStatus = FakeSessionStatus
    sys.modules["optv.parliaments.XX.common"] = common


def _write_fake_workflow_script(tools_dir: Path) -> None:
    """Write a runnable `optv/parliaments/XX/workflow.py` that the subprocess
    will execute. Emits `Publishing NNN` lines that drive progress regex."""
    pkg = tools_dir / "optv" / "parliaments" / "XX"
    pkg.mkdir(parents=True, exist_ok=True)
    # Empty __init__.py files so Python treats this as a regular package
    # (avoids namespace-package merging with the real Tools install).
    (tools_dir / "optv" / "__init__.py").touch()
    (tools_dir / "optv" / "parliaments" / "__init__.py").touch()
    (pkg / "__init__.py").touch()
    (pkg / "workflow.py").write_text(textwrap.dedent("""
        import argparse
        import sys
        parser = argparse.ArgumentParser()
        parser.add_argument("data_dir")
        parser.add_argument("--period", type=int)
        parser.add_argument("--retry-count", type=int)
        parser.add_argument("--cache-dir")
        parser.add_argument("--lang")
        parser.add_argument("--limit-session", default="")
        parser.add_argument("--ner-api-endpoint", default="")
        parser.add_argument("--align-timeout", type=int, default=1200)
        parser.add_argument("--align-max-audio-seconds", type=int, default=2400)
        parser.add_argument("--limit-to-period", action=argparse.BooleanOptionalAction, default=True)
        parser.add_argument("--no-single-instance", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--download-original", action="store_true")
        parser.add_argument("--merge-speeches", action="store_true")
        parser.add_argument("--align-sentences", action="store_true")
        parser.add_argument("--link-entities", action="store_true")
        parser.add_argument("--update-nel-entities", action="store_true")
        parser.add_argument("--nel-entity-url", default="")
        parser.add_argument("--extract-entities", action="store_true")
        args = parser.parse_args()
        for s in ["21001", "21002", "21003"]:
            print(f"INFO Publishing {s} from {s}-merged.json", flush=True)
        sys.exit(0)
    """))


def _make_app_config(tmp_path: Path, tools_dir: Path, data_dir: Path) -> AppConfig:
    settings = Settings(
        GITHUB_CLIENT_ID="x",
        GITHUB_CLIENT_SECRET="x",
        JWT_SECRET="x" * 32,
        CONFIG_DIR=str(tmp_path),
        STATUS_DIR=str(tmp_path / "status"),
        DATA_DIR=str(tmp_path),
    )
    app_config = AppConfig.__new__(AppConfig)
    app_config.settings = settings
    app_config.config_dir = tmp_path
    app_config.status_dir = tmp_path / "status"
    app_config.data_dir = tmp_path
    app_config.parliaments = {
        "XX": ParliamentConfig(
            name="Fake Parliament",
            language="deu",
            tools_dir=str(tools_dir),
            data_dir=str(data_dir),
            current_period=21,
            periods=[21],
            stages=ParliamentStages(),
        )
    }
    app_config.users = {}
    app_config.schedules = {}
    app_config.notifications = types.SimpleNamespace(slack=SlackConfig())
    return app_config


def _install_fake_common_with_statuses(parliament: str, sessions: dict) -> None:
    """Register a fake `optv.parliaments.<p>.common` whose Config.status returns
    a caller-supplied set of status names per session. `is_newer` is always True
    (mimics the merge stage having just bumped the merged cache)."""
    common = types.ModuleType(f"optv.parliaments.{parliament}.common")

    class FakeSessionStatus(enum.Enum):
        linked = "linked"
        aligned = "aligned"
        ner = "ner"
        no_text = "no_text"

    class FakeConfig:
        def __init__(self, data_dir):
            self.data_dir = Path(data_dir)
        def sessions(self, prefix=""):
            return list(sessions)
        def is_newer(self, session, stage, than):
            return True
        def status(self, session):
            return {FakeSessionStatus[name] for name in sessions[session]}

    common.Config = FakeConfig
    common.SessionStatus = FakeSessionStatus
    sys.modules[f"optv.parliaments.{parliament}.common"] = common


def test_estimate_total_skips_already_aligned_and_no_text(tmp_path):
    # 21001 already aligned, 21003 media-only (no_text) — neither should align
    # again even though is_newer() is True (merge bumped the merged cache). Only
    # 21002, which lacks the aligned flag, is real align work.
    _install_fake_common_with_statuses("YY", {
        "21001": {"linked", "aligned"},
        "21002": {"linked"},
        "21003": {"linked", "no_text"},
    })
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app_config = _make_app_config(tmp_path, tmp_path / "tools", data_dir)
    app_config.parliaments["YY"] = app_config.parliaments.pop("XX")
    runner = WorkflowRunner(app_config, JobManager(tmp_path / "status"),
                            LogStreamer(tmp_path / "status"), notifier=None)

    job = Job.new(parliament="YY", stages=["align"], period=21)
    assert runner._estimate_total_sessions(job, app_config.parliaments["YY"]) == 1


def test_apply_log_patterns_counts_each_stage_per_session(tmp_path):
    """A nel+align+ner run touches every session once per stage, so the counter
    must advance through all three stages instead of hitting 100% during nel and
    freezing — and `current_session` must follow the session being worked now,
    not stay stuck on the last-linked session. Regression for the bug where a
    bare-session `seen` set deduped across stages.
    """
    app_config = _make_app_config(tmp_path, tmp_path / "tools", tmp_path / "data")
    runner = WorkflowRunner(app_config, JobManager(tmp_path / "status"),
                            LogStreamer(tmp_path / "status"), notifier=None)
    job = Job.new(parliament="XX", stages=["nel", "align", "ner"], period=21)
    job.sessions_total = 6  # 2 sessions × 3 stages
    seen: set[tuple[str, str]] = set()

    lines = [
        "INFO Linking entities with wikidata IDs (2 session(s))",
        "WARNING Linking entities for 27045 from 27045-merged.json",
        "WARNING Publishing 27045 from 27045-merged.json",
        "WARNING Linking entities for 27276 from 27276-merged.json",
        "WARNING Publishing 27276 from 27276-merged.json",
        "INFO Updating time-alignment for merged files (2 session(s))",
        "WARNING Time-aligning 27045",
        "WARNING Publishing 27045 from 27045-aligned.json",
        "WARNING Time-aligning 27276",
        "WARNING Publishing 27276 from 27276-aligned.json",
        "INFO Updating NER for published sessions (2 session(s))",
        "WARNING Extracting Named Entities for 27045 from 27045-aligned.json",
        "WARNING Extracting Named Entities for 27276 from 27276-aligned.json",
    ]
    for line in lines:
        runner._apply_log_patterns(job, line, seen)

    # All six (stage, session) work-units counted — not deduped to 2.
    assert job.sessions_completed == 6
    assert job.progress == 100
    assert job.stage == "ner"
    # The spinner followed the latest line, not the last nel session.
    assert job.current_session == "27276"

    # Mid-align snapshot: nel done (2) + first align (1) = 3/6, current = 27045.
    job2 = Job.new(parliament="XX", stages=["nel", "align", "ner"], period=21)
    job2.sessions_total = 6
    seen2: set[tuple[str, str]] = set()
    for line in lines[:7]:  # through "Time-aligning 27045"
        runner._apply_log_patterns(job2, line, seen2)
    assert job2.stage == "align"
    assert job2.current_session == "27045"
    assert job2.sessions_completed == 3


@pytest.mark.asyncio
async def test_runner_streams_logs_and_updates_progress(tmp_path):
    tools_dir = tmp_path / "tools"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_fake_workflow_script(tools_dir)
    _install_fake_common_in_sys_modules()

    app_config = _make_app_config(tmp_path, tools_dir, data_dir)

    jm = JobManager(tmp_path / "status")
    streamer = LogStreamer(tmp_path / "status")
    runner = WorkflowRunner(app_config, jm, streamer, notifier=None)

    job = Job.new(parliament="XX", stages=["merge"], period=21)
    jm.enqueue(job)
    popped = jm.dequeue()
    assert popped is not None
    await runner.run_job(popped)

    log_text = streamer.read(popped.id)
    for s in ["21001", "21002", "21003"]:
        assert f"Publishing {s}" in log_text

    history = jm.list_history()
    assert len(history) == 1
    completed = history[0]
    assert completed["status"] == "completed"
    assert completed["sessions_total"] == 3
    assert completed["sessions_completed"] == 3
    assert completed["progress"] == 100


def test_build_argv_limit_to_period_depends_on_session_filter(tmp_path):
    """A session-filtered job must disable the period prefix filter.

    `--limit-to-period` is a DE-only ID convention; when `session_filter`
    pins exact sessions it is redundant and breaks cross-period reruns.
    """
    tools_dir = tmp_path / "tools"
    data_dir = tmp_path / "data"
    app_config = _make_app_config(tmp_path, tools_dir, data_dir)
    parliament = app_config.parliaments["XX"]
    runner = WorkflowRunner(app_config, JobManager(tmp_path / "status"),
                            LogStreamer(tmp_path / "status"), notifier=None)

    whole_period = Job.new(parliament="XX", stages=["ner"], period=21)
    argv = runner._build_argv(whole_period, parliament, ["ner"])
    assert "--limit-to-period" in argv
    assert "--no-limit-to-period" not in argv

    targeted = Job.new(parliament="XX", stages=["ner"], period=21,
                       session_filter="^(20205)$")
    argv = runner._build_argv(targeted, parliament, ["ner"])
    assert "--no-limit-to-period" in argv
    assert "--limit-to-period" not in argv


def test_build_argv_emits_force_and_rebuild_flags(tmp_path):
    """A manual re-run always forces; --rebuild is added only when requested
    (and implies --force in Tools, so both appear)."""
    tools_dir = tmp_path / "tools"
    data_dir = tmp_path / "data"
    app_config = _make_app_config(tmp_path, tools_dir, data_dir)
    parliament = app_config.parliaments["XX"]
    runner = WorkflowRunner(app_config, JobManager(tmp_path / "status"),
                            LogStreamer(tmp_path / "status"), notifier=None)

    force_only = Job.new(parliament="XX", stages=["ner"], period=21, force=True)
    argv = runner._build_argv(force_only, parliament, ["ner"])
    assert "--force" in argv
    assert "--rebuild" not in argv

    rebuilding = Job.new(parliament="XX", stages=["ner"], period=21,
                         force=True, rebuild=True)
    argv = runner._build_argv(rebuilding, parliament, ["ner"])
    assert "--force" in argv
    assert "--rebuild" in argv


def test_build_argv_refreshes_entity_registry_every_job(tmp_path):
    """Every job requests an entity-dump refresh, independent of the nel stage,
    restoring the behaviour legacy `optv pull` had via curl (it re-fetched the
    dump unconditionally). `--update-nel-entities` is its own Tools stage,
    decoupled from `--link-entities`."""
    tools_dir = tmp_path / "tools"
    data_dir = tmp_path / "data"
    app_config = _make_app_config(tmp_path, tools_dir, data_dir)
    parliament = app_config.parliaments["XX"]
    runner = WorkflowRunner(app_config, JobManager(tmp_path / "status"),
                            LogStreamer(tmp_path / "status"), notifier=None)
    job = Job.new(parliament="XX", stages=["nel"], period=21)

    argv = runner._build_argv(job, parliament, ["nel"])
    assert "--link-entities" in argv
    assert "--update-nel-entities" in argv
    # No override configured -> workflow falls back to the Tools manifest URL.
    assert not any(a.startswith("--nel-entity-url=") for a in argv)

    # A non-nel job still refreshes the registry, but does not link entities.
    download_only = runner._build_argv(job, parliament, ["download"])
    assert "--update-nel-entities" in download_only
    assert "--link-entities" not in download_only

    # A configured override is passed through to workflow.py.
    parliament.entity_dump_url = "https://example.org/dump.json"
    argv = runner._build_argv(job, parliament, ["nel"])
    assert "--nel-entity-url=https://example.org/dump.json" in argv


def _patch_pids_usage(monkeypatch, fake) -> None:
    """Replace the runner module's `_pids_usage` for one test.

    Patched through `WorkflowRunner`'s own globals rather than by dotted name:
    test_api.py drops every `src.*` entry from sys.modules to re-import under
    different env vars, so `monkeypatch.setattr("src.workflow.runner...")` can
    patch a freshly imported module object while the `WorkflowRunner` this file
    imported still resolves `_pids_usage` from the original one.
    """
    monkeypatch.setitem(WorkflowRunner.run_job.__globals__, "_pids_usage", fake)


def _runner_with_schedule(tmp_path, monkeypatch, *, pids=(4090, 4096)):
    """Runner whose host reports almost no free pids, plus a `nightly` schedule."""
    tools_dir = tmp_path / "tools"
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    _write_fake_workflow_script(tools_dir)
    _install_fake_common_in_sys_modules()

    app_config = _make_app_config(tmp_path, tools_dir, data_dir)
    app_config.schedules = {
        "nightly": ScheduleConfig(parliament="XX", cron="*/5 * * * *", stages=["merge"])
    }
    _patch_pids_usage(monkeypatch, lambda: pids)

    jm = JobManager(tmp_path / "status")
    streamer = LogStreamer(tmp_path / "status")
    return WorkflowRunner(app_config, jm, streamer, notifier=None), jm, streamer, app_config


@pytest.mark.asyncio
async def test_pipeline_refuses_to_start_without_process_headroom(tmp_path, monkeypatch):
    """A host that can't fork fails the job immediately, before spawning.

    The real failure mode buried this in an OpenBLAS thread error and a numpy
    traceback from inside the workflow — unreadable, and misattributed to the
    pipeline.
    """
    runner, jm, streamer, _ = _runner_with_schedule(tmp_path, monkeypatch)

    job = Job.new(parliament="XX", stages=["merge"], period=21)
    jm.enqueue(job)
    popped = jm.dequeue()
    await runner.run_job(popped)

    assert popped.status == "failed"
    assert "process slots" in popped.error
    assert "pids 4090/4096" in popped.error
    # The workflow subprocess never ran, so none of its output is in the log.
    assert "Publishing" not in streamer.read(popped.id)


@pytest.mark.asyncio
async def test_schedule_pauses_itself_after_repeated_failures(tmp_path, monkeypatch):
    runner, jm, streamer, app_config = _runner_with_schedule(tmp_path, monkeypatch)
    sched = app_config.schedules["nightly"]

    async def run_one():
        job = Job.new(parliament="XX", stages=["merge"], period=21)
        job.schedule_id = "nightly"
        job.source = "scheduled"
        jm.enqueue(job)
        popped = jm.dequeue()
        await runner.run_job(popped)
        return popped

    first = await run_one()
    assert first.status == "failed"
    assert sched.enabled is True  # one failure is not a pattern

    await run_one()
    assert sched.enabled is True

    third = await run_one()
    assert sched.enabled is False
    assert "paused automatically after 3 consecutive failures" in streamer.read(third.id)

    # Persisted the way the UI persists it, so the pause survives a restart.
    written = yaml.safe_load((tmp_path / "schedules.yaml").read_text())
    assert written["schedules"]["nightly"]["enabled"] is False


@pytest.mark.asyncio
async def test_healthy_host_is_not_blocked_by_the_preflight(tmp_path, monkeypatch):
    """Plenty of headroom (and an absent pids cgroup) must both run normally."""
    runner, jm, _, _ = _runner_with_schedule(tmp_path, monkeypatch, pids=(50, 4096))
    job = Job.new(parliament="XX", stages=["merge"], period=21)
    jm.enqueue(job)
    popped = jm.dequeue()
    await runner.run_job(popped)
    assert popped.status == "completed"

    _patch_pids_usage(monkeypatch, lambda: None)
    job2 = Job.new(parliament="XX", stages=["merge"], period=21)
    jm.enqueue(job2)
    popped2 = jm.dequeue()
    await runner.run_job(popped2)
    assert popped2.status == "completed"


@pytest.mark.asyncio
async def test_fork_failure_during_git_pull_is_reported_as_host_exhaustion(tmp_path, monkeypatch):
    """A host too full to fork `git` must not surface as a bare BlockingIOError.

    Observed in production: the pids cgroup read as unlimited, so the preflight
    passed, and the job died on the best-effort `git pull` with
    `BlockingIOError: [Errno 11] Resource temporarily unavailable` and no clue
    which command failed or why.
    """
    runner, jm, streamer, _ = _runner_with_schedule(tmp_path, monkeypatch, pids=None)

    async def refuse_to_fork(*_args, **_kwargs):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_to_fork)

    job = Job.new(parliament="XX", stages=["merge"], period=21)
    jm.enqueue(job)
    popped = jm.dequeue()
    await runner.run_job(popped)

    assert popped.status == "failed"
    assert "HostResourceError" in popped.error
    assert "host cannot start new processes" in popped.error
    assert "git pull --ff-only" in popped.error


@pytest.mark.asyncio
async def test_missing_git_binary_still_only_warns(tmp_path, monkeypatch):
    """Pulling stays best-effort for ordinary errors — only exhaustion aborts."""
    runner, jm, streamer, _ = _runner_with_schedule(tmp_path, monkeypatch, pids=None)
    real_exec = asyncio.create_subprocess_exec

    async def fail_git_only(*args, **kwargs):
        if args and args[0] == "git":
            raise FileNotFoundError(2, "No such file or directory")
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_git_only)

    job = Job.new(parliament="XX", stages=["merge"], period=21)
    jm.enqueue(job)
    popped = jm.dequeue()
    await runner.run_job(popped)

    assert popped.status == "completed"
    assert "git not found" in streamer.read(popped.id)
