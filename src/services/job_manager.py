"""File-backed job queue.

State files under `status/jobs/`:
  - queue.json    — list of pending jobs, FIFO
  - current.json  — the in-progress job, or null
  - history/<id>.json — one file per completed/cancelled/failed job

All writes are guarded with `fcntl.flock` on the parent directory so
concurrent readers/writers (web workers + scheduler) stay consistent.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    parliament: str
    stages: list[str]
    period: int | None = None
    session_filter: str | None = None
    force: bool = False
    source: str = "manual"  # "manual" | "scheduled"
    schedule_id: str | None = None
    publish_on_success: bool = False
    created_at: str = field(default_factory=_utcnow)
    started_at: str | None = None
    finished_at: str | None = None
    status: str = "queued"  # queued | running | completed | partial | failed | cancelled
    stage: str | None = None
    progress: int = 0  # 0..100
    current_session: str | None = None
    sessions_total: int = 0
    sessions_completed: int = 0
    failed_sessions: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def new(cls, **kwargs: Any) -> "Job":
        return cls(id=str(uuid.uuid4()), **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobManager:
    def __init__(self, status_dir: Path) -> None:
        self.root = Path(status_dir) / "jobs"
        self.queue_file = self.root / "queue.json"
        self.current_file = self.root / "current.json"
        self.history_dir = self.root / "history"
        self.lock_file = self.root / ".lock"
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        if not self.queue_file.exists():
            self._write(self.queue_file, [])
        if not self.current_file.exists():
            self._write(self.current_file, None)
        self.lock_file.touch(exist_ok=True)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock_file.open("a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path) -> Any:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        tmp.replace(path)

    # --- Queue operations ---

    def enqueue(self, job: Job) -> int:
        """Append `job` to the queue. Returns 1-based position."""
        with self._locked():
            queue = self._read(self.queue_file) or []
            queue.append(job.to_dict())
            self._write(self.queue_file, queue)
            return len(queue)

    def dequeue(self) -> Job | None:
        """Pop the next queued job and set it as current. Returns None if empty."""
        with self._locked():
            queue = self._read(self.queue_file) or []
            if not queue:
                return None
            head = queue.pop(0)
            self._write(self.queue_file, queue)
            head["status"] = "running"
            head["started_at"] = _utcnow()
            self._write(self.current_file, head)
            return Job(**head)

    def set_current(self, job: Job) -> None:
        """Update the current job's live state."""
        with self._locked():
            self._write(self.current_file, job.to_dict())

    def current(self) -> Job | None:
        raw = self._read(self.current_file)
        return Job(**raw) if raw else None

    def complete(self, job: Job) -> None:
        """Move the current job into history."""
        job.finished_at = _utcnow()
        with self._locked():
            self._write(self.current_file, None)
            self._write(self.history_dir / f"{job.id}.json", job.to_dict())

    def cancel(self, job_id: str) -> bool:
        """Cancel a job. Returns True if cancelled from the queue (not-yet-started).

        For a running job, sets a cancellation marker on the current entry and
        lets the runner observe it cooperatively.
        """
        with self._locked():
            queue = self._read(self.queue_file) or []
            for i, entry in enumerate(queue):
                if entry["id"] == job_id:
                    entry["status"] = "cancelled"
                    entry["finished_at"] = _utcnow()
                    queue.pop(i)
                    self._write(self.queue_file, queue)
                    self._write(self.history_dir / f"{job_id}.json", entry)
                    return True
            current = self._read(self.current_file)
            if current and current["id"] == job_id:
                current["status"] = "cancelling"
                self._write(self.current_file, current)
                return False
        return False

    def active_schedule_ids(self) -> set[str]:
        """schedule_ids of jobs currently running or still queued.

        Used by the scheduler to coalesce: a recurring cron must not pile up
        a fresh job each firing while its previous run is still in flight.
        """
        ids: set[str] = set()
        current = self._read(self.current_file)
        if current and current.get("schedule_id"):
            ids.add(current["schedule_id"])
        for entry in self._read(self.queue_file) or []:
            if entry.get("schedule_id"):
                ids.add(entry["schedule_id"])
        return ids

    def clear_queue(self, parliament: str | None = None) -> int:
        """Drop not-yet-started queued jobs (the running job is untouched).

        Scoped to `parliament` when given. Each dropped job is recorded in
        history as cancelled, mirroring single-job `cancel`. Returns the count.
        """
        with self._locked():
            queue = self._read(self.queue_file) or []
            removed = [e for e in queue if parliament is None or e.get("parliament") == parliament]
            if not removed:
                return 0
            kept = [e for e in queue if e not in removed]
            self._write(self.queue_file, kept)
            for entry in removed:
                entry["status"] = "cancelled"
                entry["finished_at"] = _utcnow()
                self._write(self.history_dir / f"{entry['id']}.json", entry)
            return len(removed)

    def clear_history(self, parliament: str | None = None) -> int:
        """Delete history files, scoped to `parliament` when given. Returns count."""
        with self._locked():
            deleted = 0
            for path in self.history_dir.glob("*.json"):
                if parliament is not None:
                    data = self._read(path)
                    if not data or data.get("parliament") != parliament:
                        continue
                path.unlink()
                deleted += 1
            return deleted

    def list_queue(self) -> list[dict[str, Any]]:
        return self._read(self.queue_file) or []

    def list_history(self, limit: int = 50) -> list[dict[str, Any]]:
        files = sorted(
            self.history_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        return [self._read(p) for p in files]

    def get(self, job_id: str) -> dict[str, Any] | None:
        current = self._read(self.current_file)
        if current and current["id"] == job_id:
            return current
        for entry in self._read(self.queue_file) or []:
            if entry["id"] == job_id:
                return entry
        path = self.history_dir / f"{job_id}.json"
        return self._read(path) if path.exists() else None
