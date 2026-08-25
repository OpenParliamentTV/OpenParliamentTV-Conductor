import os
import time
from pathlib import Path

from src.services.job_manager import Job, JobManager


def test_enqueue_dequeue_fifo(tmp_path):
    jm = JobManager(tmp_path)
    j1 = Job.new(parliament="DE", stages=["merge"])
    j2 = Job.new(parliament="DE", stages=["nel"])
    j3 = Job.new(parliament="DE", stages=["align"])
    assert jm.enqueue(j1) == 1
    assert jm.enqueue(j2) == 2
    assert jm.enqueue(j3) == 3

    popped = jm.dequeue()
    assert popped is not None and popped.id == j1.id
    assert jm.current().id == j1.id
    popped.sessions_total = 2
    popped.sessions_completed = 2
    popped.status = "completed"
    jm.complete(popped)
    assert jm.current() is None

    popped = jm.dequeue()
    assert popped is not None and popped.id == j2.id


def test_history_persistence_across_instances(tmp_path):
    jm = JobManager(tmp_path)
    j = Job.new(parliament="DE", stages=["merge"])
    jm.enqueue(j)
    popped = jm.dequeue()
    popped.status = "completed"
    jm.complete(popped)

    jm2 = JobManager(tmp_path)
    history = jm2.list_history()
    assert len(history) == 1 and history[0]["id"] == j.id


def test_cancel_queued_job(tmp_path):
    jm = JobManager(tmp_path)
    a = Job.new(parliament="DE", stages=["merge"])
    b = Job.new(parliament="DE", stages=["nel"])
    jm.enqueue(a)
    jm.enqueue(b)
    assert jm.cancel(b.id) is True
    assert [e["id"] for e in jm.list_queue()] == [a.id]
    history = jm.list_history()
    assert any(e["id"] == b.id and e["status"] == "cancelled" for e in history)


def test_cancel_running_job_marks_cancelling(tmp_path):
    jm = JobManager(tmp_path)
    a = Job.new(parliament="DE", stages=["merge"])
    jm.enqueue(a)
    jm.dequeue()
    assert jm.cancel(a.id) is False
    assert jm.current().status == "cancelling"


def test_clear_queue_scoped_to_parliament(tmp_path):
    jm = JobManager(tmp_path)
    de1 = Job.new(parliament="DE", stages=["merge"])
    de2 = Job.new(parliament="DE", stages=["nel"])
    at1 = Job.new(parliament="AT", stages=["merge"])
    for j in (de1, de2, at1):
        jm.enqueue(j)
    assert jm.clear_queue(parliament="DE") == 2
    assert [e["id"] for e in jm.list_queue()] == [at1.id]
    history = jm.list_history()
    assert {e["id"] for e in history if e["status"] == "cancelled"} == {de1.id, de2.id}


def test_clear_queue_leaves_running_job(tmp_path):
    jm = JobManager(tmp_path)
    running = Job.new(parliament="DE", stages=["merge"])
    queued = Job.new(parliament="DE", stages=["nel"])
    jm.enqueue(running)
    jm.enqueue(queued)
    jm.dequeue()  # running -> current
    assert jm.clear_queue(parliament="DE") == 1
    assert jm.list_queue() == []
    assert jm.current().id == running.id


def test_clear_history_scoped_to_parliament(tmp_path):
    jm = JobManager(tmp_path)
    for pid in ("DE", "DE", "AT"):
        j = Job.new(parliament=pid, stages=["merge"])
        jm.enqueue(j)
        popped = jm.dequeue()
        popped.status = "completed"
        jm.complete(popped)
    assert jm.clear_history(parliament="DE") == 2
    remaining = jm.list_history()
    assert all(e["parliament"] == "AT" for e in remaining)
    assert len(remaining) == 1


def _finish(jm: JobManager, *, parliament: str = "DE", status: str = "completed",
            schedule_id: str | None = None) -> str:
    """Run one job through the queue to history and return its id."""
    job = Job.new(parliament=parliament, stages=["merge"])
    job.schedule_id = schedule_id
    jm.enqueue(job)
    popped = jm.dequeue()
    popped.status = status
    popped.schedule_id = schedule_id
    jm.complete(popped)
    return popped.id


def _write_log(jm: JobManager, job_id: str) -> Path:
    """Create the log file LogStreamer would have written for `job_id`."""
    jm.log_dir.mkdir(parents=True, exist_ok=True)
    path = jm.log_dir / f"{job_id}.log"
    path.write_text("some log output\n", encoding="utf-8")
    return path


def test_history_pruning_keeps_newest_and_deletes_their_logs(tmp_path):
    jm = JobManager(tmp_path, history_max_entries=3)
    ids = []
    for i in range(6):
        job_id = _finish(jm)
        path = _write_log(jm, job_id)
        # Explicit, strictly increasing mtimes — same-second writes would
        # otherwise make "which are the newest 3" filesystem-dependent.
        stamp = time.time() - (10 - i)
        os.utime(jm.history_dir / f"{job_id}.json", (stamp, stamp))
        os.utime(path, (stamp, stamp))
        ids.append(job_id)

    # complete() prunes, but the last job's mtime was rewritten after its own
    # prune ran; prune once more now that the ordering is pinned.
    with jm._locked():
        jm._prune_history_locked()

    kept = {e["id"] for e in jm.list_history(limit=100)}
    assert kept == set(ids[-3:])
    for dropped in ids[:-3]:
        assert not (jm.history_dir / f"{dropped}.json").exists()
        assert not (jm.log_dir / f"{dropped}.log").exists()
    for keep in ids[-3:]:
        assert (jm.log_dir / f"{keep}.log").exists()


def test_history_pruning_drops_entries_past_max_age(tmp_path):
    jm = JobManager(tmp_path, history_max_entries=100, history_max_age_days=30)
    fresh = _finish(jm)
    stale = _finish(jm)
    _write_log(jm, fresh)
    _write_log(jm, stale)
    old = time.time() - 31 * 86400
    os.utime(jm.history_dir / f"{stale}.json", (old, old))

    with jm._locked():
        assert jm._prune_history_locked() == 1

    assert {e["id"] for e in jm.list_history(limit=100)} == {fresh}
    assert not (jm.log_dir / f"{stale}.log").exists()


def test_clear_history_removes_job_logs(tmp_path):
    jm = JobManager(tmp_path)
    job_id = _finish(jm)
    log = _write_log(jm, job_id)
    assert jm.clear_history() == 1
    assert not log.exists()


def test_consecutive_failures_stops_at_the_last_success(tmp_path):
    jm = JobManager(tmp_path)
    _finish(jm, status="failed", schedule_id="nightly")
    _finish(jm, status="completed", schedule_id="nightly")
    for _ in range(2):
        _finish(jm, status="failed", schedule_id="nightly")
    # Another schedule's failures must not be counted into this one.
    _finish(jm, status="failed", schedule_id="other")

    assert jm.consecutive_failures("nightly") == 2
    assert jm.consecutive_failures("other") == 1
    assert jm.consecutive_failures("unknown") == 0


def test_pruning_reclaims_logs_orphaned_by_an_earlier_clear(tmp_path):
    """Logs whose history entry is already gone still get collected.

    Before retention existed, `clear_history` deleted history files only — a
    long-lived deployment accumulates logs nothing will ever match to a job.
    """
    jm = JobManager(tmp_path)
    orphan = _write_log(jm, "job-from-a-previous-life")

    kept_id = _finish(jm)
    _write_log(jm, kept_id)
    with jm._locked():
        jm._prune_history_locked()

    assert not orphan.exists()
    assert (jm.log_dir / f"{kept_id}.log").exists()


def test_pruning_keeps_the_running_and_queued_jobs_logs(tmp_path):
    """The live job writes its log long before it has a history entry."""
    jm = JobManager(tmp_path)
    running = Job.new(parliament="DE", stages=["merge"])
    queued = Job.new(parliament="DE", stages=["nel"])
    jm.enqueue(running)
    jm.enqueue(queued)
    popped = jm.dequeue()  # running -> current.json, no history file yet
    running_log = _write_log(jm, popped.id)
    queued_log = _write_log(jm, queued.id)

    with jm._locked():
        jm._prune_history_locked()

    assert running_log.exists()
    assert queued_log.exists()
