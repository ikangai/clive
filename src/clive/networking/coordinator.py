# coordinator.py
"""Coordinated task splitting across local clive instances.

When a task is too large for one instance, the coordinator splits it
into sub-tasks, dispatches them via the job queue, and aggregates results.
"""

import logging
from dataclasses import dataclass, field

from server.queue import JobQueue, Job, JobStatus

log = logging.getLogger(__name__)


@dataclass
class SplitResult:
    job_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"Dispatched {len(self.job_ids)} sub-tasks: {', '.join(self.job_ids)}"


class TaskCoordinator:
    """Coordinates task splitting and result aggregation via the job queue."""

    def __init__(self, queue: JobQueue):
        self.queue = queue

    def dispatch(self, subtasks: list[str], user: str, toolset: str = "minimal") -> SplitResult:
        """Split and enqueue sub-tasks. Returns SplitResult with job IDs."""
        result = SplitResult()
        for task_desc in subtasks:
            job = self.queue.enqueue(task=task_desc, user=user, toolset=toolset)
            result.job_ids.append(job.id)
            log.info("Dispatched sub-task %s: %s", job.id, task_desc[:80])
        return result

    def collect(self, job_ids: list[str]) -> list[Job | None]:
        """Collect results for dispatched sub-tasks. Returns list of Jobs."""
        results = []
        for job_id in job_ids:
            job = self.queue.get(job_id)
            results.append(job)
        return results

    def wait_and_collect(self, job_ids: list[str], timeout: float = 300.0, poll_interval: float = 1.0) -> list[Job | None]:
        """Wait for all sub-tasks to complete and return results.

        Blocks until all jobs are done or timeout is reached.
        """
        import time
        start = time.time()
        while time.time() - start < timeout:
            jobs = self.collect(job_ids)
            if all(j and j.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED) for j in jobs):
                return jobs
            time.sleep(poll_interval)
        # Return whatever we have at timeout
        return self.collect(job_ids)
