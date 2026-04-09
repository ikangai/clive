# tests/test_reload.py
import os
import signal
import time
import threading
import multiprocessing
from server.reload import ReloadCoordinator

def test_reload_sets_pending_flag():
    """Triggering reload must set the pending flag."""
    rc = ReloadCoordinator()
    assert not rc.reload_pending
    rc.trigger_reload()
    assert rc.reload_pending

def test_reload_clears_after_acknowledge():
    """After all workers acknowledge, pending flag must clear."""
    rc = ReloadCoordinator(num_workers=2)
    rc.trigger_reload()
    rc.acknowledge(worker_id=1)
    assert rc.reload_pending  # still pending, only 1 of 2 acknowledged
    rc.acknowledge(worker_id=2)
    assert not rc.reload_pending

def test_reload_signal_triggers_reload():
    """SIGUSR1 must cause the coordinator to set reload pending."""
    rc = ReloadCoordinator()
    rc.install_signal_handler()
    # Send SIGUSR1 to self
    os.kill(os.getpid(), signal.SIGUSR1)
    time.sleep(0.1)
    assert rc.reload_pending

def test_worker_should_restart_when_reload_pending():
    """Worker check method must return True when reload is pending."""
    rc = ReloadCoordinator()
    assert not rc.should_worker_restart(worker_id=1)
    rc.trigger_reload()
    assert rc.should_worker_restart(worker_id=1)
