# server/worker.py
"""Worker process that polls the job queue and executes clive tasks."""

import logging
import os
import signal
import subprocess
import time

from server.queue import JobQueue, JobStatus

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, queue: JobQueue, max_jobs: int = 50,
                 poll_interval: float = 1.0, dry_run: bool = False):
        self.queue = queue
        self.max_jobs = max_jobs
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self.completed = 0
        self._running = True

    def run(self):
        """Main worker loop: poll queue, execute jobs, repeat."""
        import threading
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._handle_signal)
        log.info("Worker %d started (max_jobs=%d)", os.getpid(), self.max_jobs)

        while self._running and self.completed < self.max_jobs:
            job = self.queue.dequeue()
            if not job:
                time.sleep(self.poll_interval)
                continue

            log.info("Worker %d picked up job %s: %s", os.getpid(), job.id, job.task[:80])
            try:
                result = self._execute(job)
                self.queue.complete(job.id, result=result, status=JobStatus.COMPLETED)
                log.info("Job %s completed", job.id)
            except Exception as e:
                log.error("Job %s failed: %s", job.id, e)
                self.queue.complete(job.id, result=str(e), status=JobStatus.FAILED)
            self.completed += 1

        log.info("Worker %d stopping after %d jobs", os.getpid(), self.completed)

    def stop(self):
        """Signal the worker to stop after current job."""
        self._running = False

    def _execute(self, job):
        """Run a clive task. In dry_run mode, just return a mock result."""
        if self.dry_run:
            return f"[dry-run] Would execute: {job.task}"

        project_root = os.path.dirname(os.path.dirname(__file__))
        result = subprocess.run(
            ["python3", os.path.join(project_root, "clive.py"),
             "--quiet", "--json", "-t", job.toolset, job.task],
            capture_output=True, text=True,
            timeout=job.timeout or 300,
            cwd=project_root,
        )
        if result.returncode != 0:
            raise RuntimeError(f"clive exited {result.returncode}: {result.stderr[:500]}")
        return result.stdout

    def _handle_signal(self, signum, frame):
        log.info("Worker %d received signal %d, stopping", os.getpid(), signum)
        self._running = False
