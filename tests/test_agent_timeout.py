# tests/test_agent_timeout.py
import time
from server.timeout import (
    StallDetector, RetryPolicy, TimeoutConfig, AgentTimeoutError
)

def test_timeout_config_defaults():
    """Default config must have sensible values."""
    cfg = TimeoutConfig()
    assert cfg.connect_timeout == 30
    assert cfg.task_timeout == 300
    assert cfg.stall_timeout == 60

def test_stall_detector_no_stall():
    """Detector must not trigger when activity is recent."""
    detector = StallDetector(stall_timeout=5.0)
    detector.record_activity()
    assert not detector.is_stalled()

def test_stall_detector_stalled():
    """Detector must trigger when no activity for stall_timeout seconds."""
    detector = StallDetector(stall_timeout=0.1)
    detector.record_activity()
    time.sleep(0.2)
    assert detector.is_stalled()

def test_stall_detector_reset():
    """Recording activity must reset the stall timer."""
    detector = StallDetector(stall_timeout=0.1)
    time.sleep(0.2)
    detector.record_activity()
    assert not detector.is_stalled()

def test_retry_policy_allows_retries():
    """Policy must allow configured number of retries."""
    policy = RetryPolicy(max_retries=2, backoff_base=0.01)
    assert policy.should_retry(attempt=0)
    assert policy.should_retry(attempt=1)
    assert not policy.should_retry(attempt=2)

def test_retry_policy_backoff():
    """Backoff delay must increase exponentially."""
    policy = RetryPolicy(max_retries=3, backoff_base=1.0)
    assert policy.get_delay(attempt=0) == 1.0
    assert policy.get_delay(attempt=1) == 2.0
    assert policy.get_delay(attempt=2) == 4.0

def test_retry_policy_zero_retries():
    """Zero retries must never allow retry."""
    policy = RetryPolicy(max_retries=0)
    assert not policy.should_retry(attempt=0)

def test_timeout_config_custom():
    """Custom config must override defaults."""
    cfg = TimeoutConfig(connect_timeout=10, task_timeout=600, stall_timeout=120)
    assert cfg.connect_timeout == 10
    assert cfg.task_timeout == 600
    assert cfg.stall_timeout == 120

def test_task_timeout_detection():
    """Task must be detected as timed out after task_timeout."""
    cfg = TimeoutConfig(task_timeout=0.1)
    detector = StallDetector(stall_timeout=cfg.stall_timeout)
    detector.task_start = time.time() - 1.0  # started 1s ago
    assert detector.is_task_timed_out(cfg.task_timeout)
