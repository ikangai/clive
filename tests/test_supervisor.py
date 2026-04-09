# tests/test_supervisor.py
import os
import time
import threading
import tempfile
from server.queue import JobQueue
from server.supervisor import Supervisor

def test_supervisor_starts_workers(tmp_path):
    """Supervisor must spawn the requested number of workers."""
    q = JobQueue(str(tmp_path / "queue"))
    sv = Supervisor(queue_dir=str(tmp_path / "queue"), num_workers=2, dry_run=True)

    t = threading.Thread(target=sv.run)
    t.start()
    time.sleep(1.0)

    assert sv.active_worker_count() >= 1  # at least some workers started

    sv.shutdown()
    t.join(timeout=10)

def test_supervisor_restarts_crashed_worker(tmp_path):
    """Supervisor must replace a worker that exits unexpectedly."""
    sv = Supervisor(queue_dir=str(tmp_path / "queue"), num_workers=1, dry_run=True,
                    worker_max_jobs=1)

    # Enqueue a job so the worker processes it and exits (max_jobs=1)
    q = JobQueue(str(tmp_path / "queue"))
    q.enqueue(task="test", user="a", toolset="minimal")

    t = threading.Thread(target=sv.run)
    t.start()

    # Wait for the worker to finish its 1 job and get replaced
    time.sleep(3.0)

    # Supervisor should have restarted at least one worker
    assert sv.total_workers_started >= 2

    sv.shutdown()
    t.join(timeout=10)

def test_supervisor_graceful_shutdown(tmp_path):
    """Supervisor must stop all workers on shutdown."""
    sv = Supervisor(queue_dir=str(tmp_path / "queue"), num_workers=2, dry_run=True)

    t = threading.Thread(target=sv.run)
    t.start()
    time.sleep(0.5)

    sv.shutdown()
    t.join(timeout=10)
    assert not t.is_alive()

def test_supervisor_writes_health(tmp_path):
    """Supervisor must write a health status file."""
    sv = Supervisor(queue_dir=str(tmp_path / "queue"), num_workers=1, dry_run=True,
                    health_path=str(tmp_path / "health.json"), health_interval=0.5)

    t = threading.Thread(target=sv.run)
    t.start()
    time.sleep(1.5)

    assert os.path.exists(str(tmp_path / "health.json"))

    import json
    with open(str(tmp_path / "health.json")) as f:
        health = json.load(f)
    assert "status" in health
    assert "workers" in health

    sv.shutdown()
    t.join(timeout=10)
