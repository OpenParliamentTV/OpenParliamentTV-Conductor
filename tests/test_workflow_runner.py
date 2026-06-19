"""Smoke test: runner spawns the parliament's `workflow` subprocess, streams
its stdout into the job log, and bumps progress on each session message."""

from __future__ import annotations

import enum
import sys
import textwrap
import types
from pathlib import Path

import pytest

from src.config import (
    AppConfig,
    ParliamentConfig,
    ParliamentStages,
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
