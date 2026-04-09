# tests/test_health.py
import json
import os
from server.health import HealthCollector
from server.queue import JobQueue, JobStatus

def test_health_collector_basic(tmp_path):
    """Health collector must return status with expected fields."""
    q = JobQueue(str(tmp_path / "queue"))
    hc = HealthCollector(queue=q, num_workers=4, uptime_start=0.0)
    health = hc.collect()
    assert health["status"] == "healthy"
    assert health["workers"] == 4
    assert "queue_depth" in health
    assert "uptime_seconds" in health

def test_health_reflects_queue_depth(tmp_path):
    """Queue depth must reflect pending jobs."""
    q = JobQueue(str(tmp_path / "queue"))
    q.enqueue(task="a", user="u", toolset="minimal")
    q.enqueue(task="b", user="u", toolset="minimal")
    hc = HealthCollector(queue=q, num_workers=2, uptime_start=0.0)
    health = hc.collect()
    assert health["queue_depth"] == 2

def test_health_reflects_completed_jobs(tmp_path):
    """Completed/failed counts must reflect job statuses."""
    q = JobQueue(str(tmp_path / "queue"))
    j1 = q.enqueue(task="ok", user="u", toolset="minimal")
    j2 = q.enqueue(task="fail", user="u", toolset="minimal")
    q.dequeue()
    q.complete(j1.id, result="done", status=JobStatus.COMPLETED)
    q.dequeue()
    q.complete(j2.id, result="err", status=JobStatus.FAILED)
    hc = HealthCollector(queue=q, num_workers=1, uptime_start=0.0)
    health = hc.collect()
    assert health["jobs_completed"] == 1
    assert health["jobs_failed"] == 1

def test_health_write_to_file(tmp_path):
    """Health must be writable to a JSON file atomically."""
    q = JobQueue(str(tmp_path / "queue"))
    hc = HealthCollector(queue=q, num_workers=2, uptime_start=0.0)
    health_path = str(tmp_path / "health.json")
    hc.write(health_path)
    assert os.path.exists(health_path)
    with open(health_path) as f:
        data = json.load(f)
    assert data["status"] == "healthy"
    assert "queue_depth" in data

def test_health_format_human_readable(tmp_path):
    """format_human() must return a readable string."""
    q = JobQueue(str(tmp_path / "queue"))
    hc = HealthCollector(queue=q, num_workers=2, uptime_start=0.0)
    text = hc.format_human()
    assert "healthy" in text.lower() or "status" in text.lower()
