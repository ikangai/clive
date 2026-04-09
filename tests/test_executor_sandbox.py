# tests/test_executor_sandbox.py
import os
from executor import _wrap_for_sandbox

def test_sandbox_wrapping_adds_script_prefix():
    cmd = "ls -la"
    wrapped = _wrap_for_sandbox(cmd, "/tmp/clive/abc123", sandboxed=True)
    assert "sandbox/run.sh" in wrapped
    assert "/tmp/clive/abc123" in wrapped

def test_sandbox_wrapping_passthrough_when_disabled():
    cmd = "ls -la"
    wrapped = _wrap_for_sandbox(cmd, "/tmp/clive/abc123", sandboxed=False)
    assert wrapped == cmd

def test_sandbox_wrapping_respects_env_var():
    """CLIVE_SANDBOX=1 env var enables sandbox even if sandboxed=False."""
    os.environ["CLIVE_SANDBOX"] = "1"
    try:
        cmd = "echo hello"
        wrapped = _wrap_for_sandbox(cmd, "/tmp/clive/abc123", sandboxed=False)
        assert "sandbox/run.sh" in wrapped
    finally:
        del os.environ["CLIVE_SANDBOX"]

def test_sandbox_wrapping_no_network_flag():
    cmd = "curl http://example.com"
    wrapped = _wrap_for_sandbox(cmd, "/tmp/clive/abc123", sandboxed=True, no_network=True)
    assert "--no-network" in wrapped
