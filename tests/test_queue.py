# tests/test_queue.py
import os, tempfile
from server.queue import JobQueue, Job, JobStatus

def test_enqueue_and_dequeue(tmp_path):
    q = JobQueue(str(tmp_path))
    job = q.enqueue(task="hello world", user="testuser", toolset="minimal")
    assert job.status == JobStatus.PENDING
    assert os.path.exists(os.path.join(str(tmp_path), f"{job.id}.json"))

    next_job = q.dequeue()
    assert next_job is not None
    assert next_job.id == job.id
    assert next_job.status == JobStatus.RUNNING

def test_dequeue_empty(tmp_path):
    q = JobQueue(str(tmp_path))
    assert q.dequeue() is None

def test_fifo_ordering(tmp_path):
    q = JobQueue(str(tmp_path))
    j1 = q.enqueue(task="first", user="a", toolset="minimal")
    j2 = q.enqueue(task="second", user="a", toolset="minimal")
    got = q.dequeue()
    assert got.id == j1.id

def test_job_completion(tmp_path):
    q = JobQueue(str(tmp_path))
    job = q.enqueue(task="test", user="a", toolset="minimal")
    q.dequeue()
    q.complete(job.id, result="done", status=JobStatus.COMPLETED)
    loaded = q.get(job.id)
    assert loaded.status == JobStatus.COMPLETED
    assert loaded.result == "done"

def test_concurrent_dequeue_no_double_dispatch(tmp_path):
    """Two workers dequeueing simultaneously must not get the same job."""
    import threading
    q = JobQueue(str(tmp_path))
    q.enqueue(task="only one", user="a", toolset="minimal")

    results = []
    def worker():
        job = q.dequeue()
        results.append(job)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    non_none = [r for r in results if r is not None]
    assert len(non_none) == 1  # exactly one worker gets the job
