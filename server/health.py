# server/health.py
"""Health endpoint and metrics collection for clive server mode."""

import json
import logging
import os
import time

log = logging.getLogger(__name__)


class HealthCollector:
    """Collects health metrics from the queue and server state."""

    def __init__(self, queue, num_workers: int = 0, uptime_start: float = 0.0):
        self.queue = queue
        self.num_workers = num_workers
        self.uptime_start = uptime_start

    def collect(self) -> dict:
        """Collect current health metrics."""
        # Count jobs by status
        pending = 0
        completed = 0
        failed = 0
        running = 0
        total_tokens = 0

        from server.queue import JobStatus
        for path in self.queue.queue_dir.glob("*.json"):
            if path.name == ".lock":
                continue
            try:
                data = json.loads(path.read_text())
                status = data.get("status", "")
                if status == "pending":
                    pending += 1
                elif status == "completed":
                    completed += 1
                elif status == "failed":
                    failed += 1
                elif status == "running":
                    running += 1
                total_tokens += data.get("tokens_used", 0)
            except (json.JSONDecodeError, OSError):
                continue

        return {
            "status": "healthy",
            "workers": self.num_workers,
            "workers_busy": running,
            "queue_depth": pending,
            "jobs_completed": completed,
            "jobs_failed": failed,
            "jobs_running": running,
            "uptime_seconds": int(time.time() - self.uptime_start) if self.uptime_start else 0,
            "total_tokens": total_tokens,
        }

    def write(self, path: str):
        """Write health to a JSON file atomically."""
        health = self.collect()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(health, f, indent=2)
            os.rename(tmp, path)
        except Exception as e:
            log.warning("Failed to write health to %s: %s", path, e)

    def format_human(self) -> str:
        """Format health as human-readable text."""
        return format_health_dict(self.collect())


def format_health_dict(h: dict) -> str:
    """Format a health dict as human-readable text. Usable without a live collector."""
    lines = [
        f"Status:     {h.get('status', 'unknown')}",
        f"Workers:    {h.get('workers', 0)} ({h.get('workers_busy', 0)} busy)",
        f"Queue:      {h.get('queue_depth', 0)} pending",
        f"Completed:  {h.get('jobs_completed', 0)}",
        f"Failed:     {h.get('jobs_failed', 0)}",
        f"Uptime:     {h.get('uptime_seconds', 0)}s",
        f"Tokens:     {h.get('total_tokens', 0):,}",
    ]
    return "\n".join(lines)
