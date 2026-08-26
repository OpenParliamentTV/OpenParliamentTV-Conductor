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
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# History retention. Every run writes one history file and one job log, and
# nothing used to delete either.
#
# Age is the policy: keep a month, which is what you want when asking "when did
# this start going wrong?". The count is only a backstop against a schedule that
# fires far more often than any of ours.
#
# DE cadence (`*/5 9-21 * * 1-5`): 12/hour x 13 hours = 156 per *weekday*, and a
# 30-day window holds ~21.4 weekdays, so ~3,350 entries — comfortably under the
# backstop, which is the point: the age bound is the one that bites. Coalescing
# (a tick is skipped while the previous run is still going) makes that an upper
# bound. Set the count anywhere near the real rate and it silently becomes the
# real policy — 500 would have been about three days.
_HISTORY_MAX_ENTRIES = 5000
_HISTORY_MAX_AGE_DAYS = 30


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
    rebuild: bool = False  # re-derive from scratch (implies force in Tools)
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
    def __init__(
        self,
        status_dir: Path,
        *,
        history_max_entries: int = _HISTORY_MAX_ENTRIES,
        history_max_age_days: int = _HISTORY_MAX_AGE_DAYS,
    ) -> None:
        self.root = Path(status_dir) / "jobs"
        self.queue_file = self.root / "queue.json"
        self.current_file = self.root / "current.json"
        self.history_dir = self.root / "history"
        self.lock_file = self.root / ".lock"
        # Mirrors LogStreamer's layout (status/logs/jobs/<id>.log). A history
        # entry and its log are one artefact with two files, so whatever
        # deletes one must delete the other — otherwise the logs outlive every
        # history purge and grow without bound.
        self.log_dir = Path(status_dir) / "logs" / "jobs"
        self.history_max_entries = history_max_entries
        self.history_max_age_days = history_max_age_days
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
        """Move the current job into history, then apply the retention window."""
        job.finished_at = _utcnow()
        with self._locked():
            self._write(self.current_file, None)
            self._write(self.history_dir / f"{job.id}.json", job.to_dict())
            self._prune_history_locked()

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

    def _delete_entry(self, path: Path) -> None:
        """Delete one history file together with the job log it belongs to."""
        path.unlink(missing_ok=True)
        (self.log_dir / f"{path.stem}.log").unlink(missing_ok=True)

    def _prune_history_locked(self) -> int:
        """Drop history past the retention window. Caller must hold the lock.

        Two bounds, whichever bites first: keep at most `history_max_entries`
        newest jobs, and drop anything older than `history_max_age_days`. Both
        are needed — the count bound alone lets an idle deployment hoard stale
        jobs, the age bound alone lets a 5-minute failure loop pile up
        thousands within its window.
        """
        try:
            entries = [(p, p.stat().st_mtime) for p in self.history_dir.glob("*.json")]
        except OSError:
            return 0
        entries.sort(key=lambda pair: pair[1], reverse=True)
        cutoff = time.time() - self.history_max_age_days * 86400
        stale = [p for p, _ in entries[self.history_max_entries :]]
        stale += [p for p, mtime in entries[: self.history_max_entries] if mtime < cutoff]

        deleted = 0
        for path in stale:
            try:
                self._delete_entry(path)
                deleted += 1
            except OSError as exc:
                logger.warning("Could not prune job history %s: %s", path.name, exc)

        deleted += self._delete_orphan_logs_locked()
        if deleted:
            logger.info("Pruned %d job history entries / logs", deleted)
        return deleted

    def _delete_orphan_logs_locked(self) -> int:
        """Delete job logs no longer backed by a job. Caller must hold the lock.

        `clear_history` used to leave the logs behind, so a deployment can carry
        years of orphans that history-driven pruning would never reach. A log is
        an orphan only if no history entry, no queue entry, and not the running
        job claim it — the running job's log exists well before its history file
        does, and must not be swept out from under it.
        """
        claimed = {p.stem for p in self.history_dir.glob("*.json")}
        current = self._read(self.current_file)
        if current:
            claimed.add(current["id"])
        claimed.update(entry["id"] for entry in self._read(self.queue_file) or [])

        deleted = 0
        try:
            logs = list(self.log_dir.glob("*.log"))
        except OSError:
            return 0
        for log in logs:
            if log.stem in claimed:
                continue
            try:
                log.unlink(missing_ok=True)
                deleted += 1
            except OSError as exc:
                logger.warning("Could not prune orphaned job log %s: %s", log.name, exc)
        return deleted

    def clear_history(self, parliament: str | None = None) -> int:
        """Delete history files, scoped to `parliament` when given. Returns count."""
        with self._locked():
            deleted = 0
            for path in self.history_dir.glob("*.json"):
                if parliament is not None:
                    data = self._read(path)
                    if not data or data.get("parliament") != parliament:
                        continue
                self._delete_entry(path)
                deleted += 1
            return deleted

    def consecutive_failures(self, schedule_id: str, limit: int = 50) -> int:
        """How many of this schedule's most recent runs failed back-to-back.

        Read from history rather than an in-memory counter so the count
        survives a container restart — a schedule hammering a broken host is
        precisely the case where the app may be restarted mid-storm.
        """
        count = 0
        for entry in self.list_history(limit=limit):
            if not entry or entry.get("schedule_id") != schedule_id:
                continue
            if entry.get("status") != "failed":
                break
            count += 1
        return count

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
