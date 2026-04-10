"""Remote agent communication — seamless clive-to-clive via SSH.

Two clive instances communicate through a tmux pane containing an SSH
session. The local agent sends natural language tasks, the remote agent
executes and returns structured results via the framed protocol.

Protocol (framed sentinels — see protocol.py):
  → (local types task description, presses enter)
  ← <<<CLIVE:progress:...>>>   step descriptions
  ← <<<CLIVE:file:...>>>       files available on remote for scp
  ← <<<CLIVE:context:...>>>    final result payload
  ← <<<CLIVE:turn:done>>>      completion signal

File transfer: after turn=done, local scp's any declared files from remote.

Architecture:
  local clive → SSH pane → remote clive --conversational → framed output → parse
"""
import os
import subprocess
import time

from output import progress
from protocol import decode_all, latest


def parse_turn_state(screen: str) -> str | None:
    """Parse the latest turn state from framed screen content.

    Returns "thinking", "waiting", "done", "failed", or None.
    """
    frame = latest(decode_all(screen), "turn")
    if frame is None:
        return None
    state = frame.payload.get("state")
    return state.lower() if isinstance(state, str) else None


def parse_question(screen: str) -> str | None:
    """Parse the latest question from framed screen content.

    Returns the question text, or None if no question frame is found
    or the question text is missing/empty.
    """
    frame = latest(decode_all(screen), "question")
    if frame is None:
        return None
    text = frame.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def parse_context(screen: str) -> dict | None:
    """Parse the latest context payload from framed screen content."""
    frame = latest(decode_all(screen), "context")
    return frame.payload if frame is not None else None


def parse_remote_files(screen: str) -> list[str]:
    """Parse all file declarations in order of appearance."""
    out = []
    for f in decode_all(screen):
        if f.kind == "file":
            name = f.payload.get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def parse_remote_progress(screen: str) -> list[str]:
    """Parse all progress declarations in order of appearance."""
    out = []
    for f in decode_all(screen):
        if f.kind == "progress":
            text = f.payload.get("text")
            if isinstance(text, str):
                out.append(text)
    return out


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
