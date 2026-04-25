from __future__ import annotations

import types
from pathlib import Path

import pytest

from src.config import (
    AppConfig,
    NotificationsConfig,
    ParliamentConfig,
    ParliamentStages,
    ScheduleConfig,
    Settings,
)
from src.services.job_manager import JobManager
from src.services.scheduler import SchedulerService, _cron_trigger_from_string


def _make_config(tmp_path: Path, schedules: dict[str, ScheduleConfig]) -> AppConfig:
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
        "DE": ParliamentConfig(
            name="Test",
            language="deu",
            tools_dir=str(tmp_path),
            data_dir=str(tmp_path / "data"),
            current_period=21,
            periods=[21],
            stages=ParliamentStages(),
        )
    }
    app_config.users = {}
    app_config.schedules = schedules
    app_config.notifications = NotificationsConfig()
    return app_config


def test_cron_trigger_parses_five_fields():
    trig = _cron_trigger_from_string("0 2 * * *")
    assert trig is not None


def test_cron_trigger_rejects_bad_count():
    with pytest.raises(ValueError):
        _cron_trigger_from_string("0 2 * *")


def test_sync_registers_enabled_and_skips_disabled(tmp_path):
    schedules = {
        "a": ScheduleConfig(enabled=True, parliament="DE", cron="0 2 * * *", stages=["merge"]),
        "b": ScheduleConfig(enabled=False, parliament="DE", cron="0 3 * * *", stages=["nel"]),
    }
    app_config = _make_config(tmp_path, schedules)
    (tmp_path / "status").mkdir()
    jm = JobManager(tmp_path / "status")
    svc = SchedulerService(app_config, jm)
    svc.sync_jobs()
    ids = {j.id for j in svc.scheduler.get_jobs()}
    assert ids == {"a"}


def test_sync_removes_when_disabled_later(tmp_path):
    schedules = {
        "a": ScheduleConfig(enabled=True, parliament="DE", cron="0 2 * * *", stages=["merge"]),
    }
    app_config = _make_config(tmp_path, schedules)
    (tmp_path / "status").mkdir()
    jm = JobManager(tmp_path / "status")
    svc = SchedulerService(app_config, jm)
    svc.sync_jobs()
    assert {j.id for j in svc.scheduler.get_jobs()} == {"a"}
    app_config.schedules["a"].enabled = False
    svc.sync_jobs()
    assert svc.scheduler.get_jobs() == []


def test_trigger_now_enqueues_job(tmp_path):
    schedules = {
        "a": ScheduleConfig(enabled=True, parliament="DE", cron="0 2 * * *",
                            stages=["merge"], publish_on_success=True),
    }
    app_config = _make_config(tmp_path, schedules)
    (tmp_path / "status").mkdir()
    jm = JobManager(tmp_path / "status")
    svc = SchedulerService(app_config, jm)
    svc.sync_jobs()
    assert svc.trigger_now("a") is True
    queue = jm.list_queue()
    assert len(queue) == 1
    assert queue[0]["source"] == "scheduled"
    assert queue[0]["schedule_id"] == "a"
    assert queue[0]["publish_on_success"] is True


def test_trigger_now_unknown_returns_false(tmp_path):
    app_config = _make_config(tmp_path, {})
    (tmp_path / "status").mkdir()
    jm = JobManager(tmp_path / "status")
    svc = SchedulerService(app_config, jm)
    svc.sync_jobs()
    assert svc.trigger_now("nope") is False
