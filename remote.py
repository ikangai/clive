"""Remote agent communication — seamless clive-to-clive via SSH.

Two clive instances communicate through a tmux pane containing an SSH
session. The local agent sends natural language tasks, the remote agent
executes and returns structured results via the DONE: protocol.

Protocol (text-based, parsed from screen):
  → (local types task description, presses enter)
  ← PROGRESS: step N of M — description
  ← FILE: filename (scp-able from remote:{session_dir}/filename)
  ← DONE: {"status": "success"|"error", "result": "...", "files": [...]}

File transfer: after DONE, local scp's any declared files from remote.

Architecture:
  local clive → SSH pane → remote clive --quiet --json → DONE: JSON → parse
"""
import json
import os
import re
import subprocess
import time

from output import progress


def parse_remote_result(screen: str) -> dict | None:
    """Parse DONE: protocol from screen content. Returns result dict or None."""
    for line in screen.splitlines():
        stripped = line.strip()
        if stripped.startswith("DONE:"):
            payload = stripped[5:].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {"status": "success", "result": payload}
    return None


def parse_remote_progress(screen: str) -> list[str]:
    """Parse PROGRESS: lines from screen content."""
    progress_lines = []
    for line in screen.splitlines():
        stripped = line.strip()
        if stripped.startswith("PROGRESS:"):
            progress_lines.append(stripped[9:].strip())
    return progress_lines


def parse_remote_files(screen: str) -> list[str]:
    """Parse FILE: declarations from screen content."""
    files = []
    for line in screen.splitlines():
        stripped = line.strip()
        if stripped.startswith("FILE:"):
            files.append(stripped[5:].strip())
    return files


def scp_file(host: str, remote_path: str, local_dir: str, key: str | None = None) -> str | None:
    """SCP a file from remote to local. Returns local path or None on failure."""
    local_path = os.path.join(local_dir, os.path.basename(remote_path))
    cmd = ["scp"]
    if key:
        cmd.extend(["-i", key])
    cmd.extend(["-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"])
    cmd.extend([f"{host}:{remote_path}", local_path])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return local_path
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def scp_files_from_result(host: str, remote_session_dir: str,
                          files: list[str], local_dir: str,
                          key: str | None = None) -> list[str]:
    """SCP multiple files from a remote clive session dir to local."""
    local_files = []
    for fname in files:
        remote_path = f"{remote_session_dir}/{fname}" if not fname.startswith("/") else fname
        local = scp_file(host, remote_path, local_dir, key)
        if local:
            local_files.append(local)
            progress(f"    Transferred: {fname} → {local}")
        else:
            progress(f"    Transfer failed: {fname}")
    return local_files


def check_remote_clive(host: str, key: str | None = None) -> dict:
    """Check if remote host has clive available. Returns status dict."""
    cmd = ["ssh"]
    if key:
        cmd.extend(["-i", key])
    cmd.extend(["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
                "python3 -c 'import clive; print(\"ok\")'"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and "ok" in result.stdout:
            return {"available": True, "host": host}
    except (subprocess.TimeoutExpired, OSError):
        pass
    return {"available": False, "host": host, "error": "clive not found or unreachable"}


def build_remote_command(task: str, toolset: str = "minimal", json_output: bool = True) -> str:
    """Build the command to run on the remote clive instance."""
    flags = "--quiet"
    if json_output:
        flags += " --json"
    if toolset != "minimal":
        flags += f" -t {toolset}"
    # Escape task for shell
    escaped_task = task.replace("'", "'\\''")
    return f"python3 clive.py {flags} '{escaped_task}'"
