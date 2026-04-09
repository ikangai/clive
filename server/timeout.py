# server/timeout.py
"""Timeout and retry configuration for remote agent communication."""

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


class AgentTimeoutError(Exception):
    """Raised when an agent operation times out."""
    pass


@dataclass
class TimeoutConfig:
    """Timeout configuration for remote agent connections."""
    connect_timeout: int = 30      # seconds to establish SSH connection
    task_timeout: int = 300        # max seconds for entire task
    stall_timeout: int = 60        # seconds without TURN:/PROGRESS: → stall


@dataclass
class RetryPolicy:
    """Retry policy with exponential backoff."""
    max_retries: int = 2
    backoff_base: float = 5.0      # base delay in seconds

    def should_retry(self, attempt: int) -> bool:
        """Whether to retry at this attempt number (0-indexed)."""
        return attempt < self.max_retries

    def get_delay(self, attempt: int) -> float:
        """Get delay before retry (exponential backoff)."""
        return self.backoff_base * (2 ** attempt)


class StallDetector:
    """Detects when a remote agent has stalled (no activity)."""

    def __init__(self, stall_timeout: float = 60.0):
        self.stall_timeout = stall_timeout
        self.last_activity = time.time()
        self.task_start = time.time()

    def record_activity(self):
        """Record that activity was observed (TURN:, PROGRESS:, screen change)."""
        self.last_activity = time.time()

    def is_stalled(self) -> bool:
        """Check if the agent has stalled."""
        return (time.time() - self.last_activity) > self.stall_timeout

    def is_task_timed_out(self, task_timeout: float) -> bool:
        """Check if the task has exceeded its timeout."""
        return (time.time() - self.task_start) > task_timeout

    def elapsed(self) -> float:
        """Seconds since task started."""
        return time.time() - self.task_start

    def idle_seconds(self) -> float:
        """Seconds since last activity."""
        return time.time() - self.last_activity
