# server/supervisor.py
"""Supervisor that manages a pool of worker processes with crash restart."""

import json
import logging
import multiprocessing
import os
import signal
import threading
import time

log = logging.getLogger(__name__)


def _worker_target(queue_dir: str, max_jobs: int, dry_run: bool):
    """Entry point for worker subprocess."""
    from server.queue import JobQueue
    from server.worker import Worker
    q = JobQueue(queue_dir)
    Worker(queue=q, max_jobs=max_jobs, dry_run=dry_run).run()


class Supervisor:
    def __init__(self, queue_dir: str, num_workers: int = 4,
                 dry_run: bool = False, worker_max_jobs: int = 50,
                 health_path: str = "", health_interval: float = 5.0):
        self.queue_dir = queue_dir
        self.num_workers = num_workers
        self.dry_run = dry_run
        self.worker_max_jobs = worker_max_jobs
        self.health_path = health_path
        self.health_interval = health_interval
        self._running = True
        self._workers: list[multiprocessing.Process] = []
        self.total_workers_started = 0
        self._start_time = 0.0

    def run(self):
        """Main supervisor loop."""
        self._start_time = time.time()
        log.info("Supervisor starting with %d workers", self.num_workers)

        # Register SIGTERM handler if on main thread
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda signum, frame: self.shutdown())

        # Ensure queue dir exists
        os.makedirs(self.queue_dir, exist_ok=True)

        # Spawn initial workers
        for _ in range(self.num_workers):
            self._workers.append(self._spawn_worker())

        # Monitor loop
        last_health = 0.0
        while self._running:
            # Check for dead workers and restart
            new_workers = []
            for w in self._workers:
                if w.is_alive():
                    new_workers.append(w)
                else:
                    w.join(timeout=1)
                    if self._running:
                        log.info("Worker %s exited, restarting", w.pid)
                        new_workers.append(self._spawn_worker())
            self._workers = new_workers

            # Write health
            if self.health_path and time.time() - last_health > self.health_interval:
                self._write_health()
                last_health = time.time()

            time.sleep(0.5)

        # Shutdown: terminate remaining workers
        for w in self._workers:
            if w.is_alive():
                w.terminate()
        for w in self._workers:
            w.join(timeout=10)

    def shutdown(self):
        self._running = False

    def active_worker_count(self) -> int:
        return sum(1 for w in self._workers if w.is_alive())

    def _spawn_worker(self) -> multiprocessing.Process:
        """Spawn a new worker process. Returns the Process object."""
        w = multiprocessing.Process(
            target=_worker_target,
            args=(self.queue_dir, self.worker_max_jobs, self.dry_run),
        )
        w.start()
        self.total_workers_started += 1
        log.info("Spawned worker %s (total started: %d)", w.pid, self.total_workers_started)
        return w

    def _write_health(self):
        try:
            health = {
                "status": "healthy" if self._running else "shutting_down",
                "workers": self.num_workers,
                "workers_alive": self.active_worker_count(),
                "total_workers_started": self.total_workers_started,
                "uptime_seconds": int(time.time() - self._start_time),
            }
            tmp = self.health_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(health, f, indent=2)
            os.rename(tmp, self.health_path)
        except Exception as e:
            log.warning("Failed to write health: %s", e)
