# tests/test_worker.py
import os
import time
import threading
import tempfile
from server.queue import JobQueue, JobStatus
from server.worker import Worker

def test_worker_processes_single_job(tmp_path):
    """Worker must pick up and complete a queued job."""
    q = JobQueue(str(tmp_path))
    job = q.enqueue(task="echo hello", user="test", toolset="minimal")

    # Run worker in thread, limit to 1 job
    w = Worker(queue=q, max_jobs=1, dry_run=True)
    t = threading.Thread(target=w.run)
    t.start()
    t.join(timeout=10)

    completed = q.get(job.id)
    assert completed.status in (JobStatus.COMPLETED, JobStatus.FAILED)

def test_worker_stops_after_max_jobs(tmp_path):
    """Worker must self-terminate after max_jobs completions."""
    q = JobQueue(str(tmp_path))
    q.enqueue(task="echo 1", user="test", toolset="minimal")
    q.enqueue(task="echo 2", user="test", toolset="minimal")
    q.enqueue(task="echo 3", user="test", toolset="minimal")

    w = Worker(queue=q, max_jobs=2, dry_run=True)
    t = threading.Thread(target=w.run)
    t.start()
    t.join(timeout=10)

    assert w.completed == 2

def test_worker_handles_empty_queue(tmp_path):
    """Worker must not crash on empty queue, should poll and stop gracefully."""
    q = JobQueue(str(tmp_path))

    w = Worker(queue=q, max_jobs=1, poll_interval=0.1)
    w._running = False  # Stop immediately
    w.run()  # Should not raise

def test_worker_graceful_shutdown(tmp_path):
    """Worker must stop when _running is set to False."""
    q = JobQueue(str(tmp_path))

    w = Worker(queue=q, max_jobs=100, poll_interval=0.1)
    t = threading.Thread(target=w.run)
    t.start()

    time.sleep(0.3)
    w.stop()
    t.join(timeout=5)
    assert not t.is_alive()
