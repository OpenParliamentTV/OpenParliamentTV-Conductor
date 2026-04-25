"""APScheduler wrapper.

Loads `config/schedules.yaml` at startup; spawns an `AsyncIOScheduler`;
registers one cron trigger per enabled schedule. A `watchfiles` task
reloads the config when schedules.yaml changes, so enabling/disabling a
schedule takes effect within seconds.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import AppConfig
from src.services.job_manager import Job, JobManager

logger = logging.getLogger(__name__)


def _cron_trigger_from_string(cron: str) -> CronTrigger:
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-field cron expression, got: {cron!r}")
    minute, hour, day, month, weekday = parts
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=weekday)


class SchedulerService:
    def __init__(self, config: AppConfig, job_manager: JobManager) -> None:
        self.config = config
        self.job_manager = job_manager
        self.scheduler = AsyncIOScheduler()
        self._watcher_task: asyncio.Task | None = None

    def _enqueue_scheduled(self, schedule_id: str) -> None:
        sched = self.config.schedules.get(schedule_id)
        if not sched or not sched.enabled:
            return
        job = Job.new(
            parliament=sched.parliament,
            stages=list(sched.stages),
            period=self.config.parliaments[sched.parliament].current_period,
            force=sched.force,
            publish_on_success=sched.publish_on_success,
            source="scheduled",
            schedule_id=schedule_id,
        )
        self.job_manager.enqueue(job)
        logger.info("Enqueued scheduled job %s (%s)", job.id, schedule_id)

    def sync_jobs(self) -> None:
        existing = {j.id for j in self.scheduler.get_jobs()}
        wanted: dict[str, object] = {}

        for sid, sched in self.config.schedules.items():
            if not sched.enabled:
                continue
            try:
                wanted[sid] = _cron_trigger_from_string(sched.cron)
            except ValueError as exc:
                logger.error("Invalid cron for schedule %s: %s", sid, exc)

        # Remove dropped/disabled.
        for jid in existing - wanted.keys():
            self.scheduler.remove_job(jid)
            logger.info("Removed scheduler job %s", jid)

        # Add or update.
        for sid, trigger in wanted.items():
            if sid in existing:
                self.scheduler.reschedule_job(sid, trigger=trigger)
            else:
                self.scheduler.add_job(
                    self._enqueue_scheduled,
                    trigger=trigger,
                    id=sid,
                    args=[sid],
                    replace_existing=True,
                )
                logger.info("Scheduler registered %s (cron=%s)", sid, self.config.schedules[sid].cron)

    async def start(self) -> None:
        self.sync_jobs()
        self.scheduler.start()
        self._watcher_task = asyncio.create_task(self._watch_config(), name="schedule-watcher")

    async def stop(self) -> None:
        if self._watcher_task:
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass
            self._watcher_task = None
        self.scheduler.shutdown(wait=False)

    async def _watch_config(self) -> None:
        try:
            from watchfiles import awatch
        except ImportError:
            logger.warning("watchfiles not installed — schedule hot-reload disabled")
            return
        schedules_file = self.config.config_dir / "schedules.yaml"
        if not schedules_file.exists():
            return
        try:
            async for _ in awatch(schedules_file):
                logger.info("schedules.yaml changed, reloading")
                self.config.reload()
                self.sync_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("schedule watcher crashed")

    # --- API operations ---

    def next_run_times(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for job in self.scheduler.get_jobs():
            nrt = job.next_run_time
            out[job.id] = nrt.isoformat() if nrt else None
        return out

    def trigger_now(self, schedule_id: str) -> bool:
        if schedule_id not in self.config.schedules:
            return False
        self._enqueue_scheduled(schedule_id)
        return True
