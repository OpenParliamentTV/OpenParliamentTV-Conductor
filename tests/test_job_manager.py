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
