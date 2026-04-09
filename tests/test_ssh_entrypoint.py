# tests/test_ssh_entrypoint.py
import os
import subprocess

ENTRYPOINT = os.path.join(os.path.dirname(__file__), "..", "server", "ssh_entrypoint.sh")

def test_entrypoint_exists_and_executable():
    assert os.path.isfile(ENTRYPOINT)
    assert os.access(ENTRYPOINT, os.X_OK)

def test_entrypoint_shows_usage_without_command():
    """With no SSH_ORIGINAL_COMMAND, must show usage and exit non-zero."""
    env = os.environ.copy()
    env.pop("SSH_ORIGINAL_COMMAND", None)
    result = subprocess.run(
        ["bash", ENTRYPOINT],
        capture_output=True, text=True, timeout=5,
        env=env,
    )
    assert result.returncode != 0
    assert "Usage" in result.stdout or "usage" in result.stdout.lower()
