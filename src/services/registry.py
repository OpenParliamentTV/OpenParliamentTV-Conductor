"""Singleton registry for app-scoped services.

Populated in `src.main.lifespan`; read via FastAPI dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.services.job_manager import JobManager
    from src.services.log_streamer import LogStreamer
    from src.services.notifier import SlackNotifier
    from src.services.worker import Worker
    from src.workflow.runner import WorkflowRunner


class Registry:
    job_manager: "JobManager | None" = None
    log_streamer: "LogStreamer | None" = None
    notifier: "SlackNotifier | None" = None
    runner: "WorkflowRunner | None" = None
    worker: "Worker | None" = None
    scheduler: "object | None" = None


registry = Registry()


def get_job_manager() -> "JobManager":
    assert registry.job_manager is not None, "JobManager not initialised"
    return registry.job_manager


def get_log_streamer() -> "LogStreamer":
    assert registry.log_streamer is not None, "LogStreamer not initialised"
    return registry.log_streamer


def get_runner() -> "WorkflowRunner":
    assert registry.runner is not None, "WorkflowRunner not initialised"
    return registry.runner
