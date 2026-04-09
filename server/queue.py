# server/queue.py
import fcntl
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    task: str
    user: str
    toolset: str
    status: JobStatus = JobStatus.PENDING
    result: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    worker_pid: int = 0
    session_dir: str = ""
    tokens_used: int = 0
    timeout: int = 300


class JobQueue:
    def __init__(self, queue_dir: str):
        self.queue_dir = Path(queue_dir)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.queue_dir / ".lock"

    def enqueue(self, task: str, user: str, toolset: str = "minimal") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], task=task, user=user, toolset=toolset)
        with self._lock():
            self._write_job(job)
        return job

    def dequeue(self) -> Job | None:
        with self._lock():
            pending = self._list_by_status(JobStatus.PENDING)
            if not pending:
                return None
            job = pending[0]  # FIFO by created_at
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            job.worker_pid = os.getpid()
            self._write_job(job)
            return job

    def complete(self, job_id: str, result: str, status: JobStatus = JobStatus.COMPLETED):
        with self._lock():
            job = self._get_unlocked(job_id)
            if job:
                job.status = status
                job.result = result
                job.completed_at = time.time()
                self._write_job(job)

    def get(self, job_id: str) -> Job | None:
        with self._lock():
            return self._get_unlocked(job_id)

    def _get_unlocked(self, job_id: str) -> Job | None:
        path = self.queue_dir / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            data["status"] = JobStatus(data["status"])
            return Job(**data)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.warning("Corrupt job file %s: %s", path, e)
            return None

    def _write_job(self, job: Job):
        path = self.queue_dir / f"{job.id}.json"
        tmp = path.with_suffix(".tmp")
        data = asdict(job)
        data["status"] = job.status.value
        tmp.write_text(json.dumps(data, indent=2))
        os.rename(str(tmp), str(path))

    def _list_by_status(self, status: JobStatus) -> list[Job]:
        jobs = []
        for path in self.queue_dir.glob("*.json"):
            if path.name == ".lock":
                continue
            try:
                data = json.loads(path.read_text())
                if data.get("status") == status.value:
                    data["status"] = JobStatus(data["status"])
                    jobs.append(Job(**data))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.warning("Skipping corrupt job file %s: %s", path, e)
                continue
        jobs.sort(key=lambda j: j.created_at)
        return jobs

    def _lock(self):
        return _FileLock(self._lock_path)


class _FileLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self._fd = open(self.path, "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()
