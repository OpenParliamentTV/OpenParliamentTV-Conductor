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
