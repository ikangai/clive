"""Hot-reload coordination for server mode.

When a self-modification is applied, the reload coordinator:
1. Sets a reload-pending flag
2. Workers check this flag between jobs
3. Workers that see the flag finish their current job, then exit
4. The supervisor detects the exit and spawns a new worker (loading new code)
5. Once all workers have restarted, the flag clears

This piggybacks on the supervisor's existing crash-restart logic —
workers exit cleanly, supervisor replaces them.
"""

import logging
import os
import signal
import threading

log = logging.getLogger(__name__)


class ReloadCoordinator:
    """Coordinates hot-reload across supervisor and workers."""

    def __init__(self, num_workers: int = 0):
        self.num_workers = num_workers
        self._lock = threading.Lock()
        self._reload_pending = False
        self._acknowledged: set[int] = set()

    @property
    def reload_pending(self) -> bool:
        with self._lock:
            return self._reload_pending

    def trigger_reload(self):
        """Signal that a reload is needed (called after selfmod apply)."""
        with self._lock:
            self._reload_pending = True
            self._acknowledged.clear()
        log.info("Reload triggered — workers will restart after current job")

    def acknowledge(self, worker_id: int):
        """Worker acknowledges it will restart."""
        with self._lock:
            self._acknowledged.add(worker_id)
            if self.num_workers > 0 and len(self._acknowledged) >= self.num_workers:
                self._reload_pending = False
                self._acknowledged.clear()
                log.info("All workers acknowledged reload — flag cleared")

    def should_worker_restart(self, worker_id: int) -> bool:
        """Check if this worker should restart (called between jobs)."""
        return self.reload_pending

    def install_signal_handler(self):
        """Install SIGUSR1 handler that triggers reload."""
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGUSR1, self._handle_signal)
        else:
            log.warning("Cannot install SIGUSR1 handler from non-main thread")

    def _handle_signal(self, signum, frame):
        self.trigger_reload()
