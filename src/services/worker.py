"""Single-worker job drainer.

Runs inside the FastAPI event loop as a long-lived task. Polls the
JobManager for queued jobs and executes them sequentially via the
WorkflowRunner. Only one job runs at a time across the app.
"""

from __future__ import annotations

import asyncio
import logging

from src.services.job_manager import JobManager
from src.workflow.runner import WorkflowRunner

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, job_manager: JobManager, runner: WorkflowRunner, poll_seconds: float = 1.0) -> None:
        self.job_manager = job_manager
        self.runner = runner
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="optv-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        logger.info("Worker started")
        while not self._stop.is_set():
            job = self.job_manager.dequeue()
            if job is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self.runner.run_job(job)
            except Exception:
                logger.exception("Unhandled error in worker loop")
        logger.info("Worker stopped")
