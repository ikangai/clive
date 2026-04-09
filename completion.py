"""Completion and intervention detection for tmux pane commands.

Completion strategies (checked in priority order):
1. Unique end marker (for shell commands)
2. Prompt sentinel: [AGENT_READY] $ on last line
3. Idle timeout: screen unchanged for N seconds

Intervention detection (for streaming observation level):
Scans screen for patterns that indicate the agent should intervene
(prompts for input, errors, confirmations). Returns early with
method="intervention" when detected.
"""

import re
import time
import uuid

from models import PaneInfo

DEFAULT_IDLE_TIMEOUT = 2.0
MAX_WAIT = 30.0

# Patterns that indicate the agent should intervene during execution
INTERVENTION_PATTERNS = [
    (re.compile(r'\[y/N\]|\[Y/n\]|\(yes/no\)'), "confirmation_prompt"),
    (re.compile(r'[Pp]assword:'), "password_prompt"),
    (re.compile(r'Are you sure'), "confirmation_prompt"),
    (re.compile(r'[Oo]verwrite.*\?'), "overwrite_prompt"),
    (re.compile(r'Press .* to continue'), "continue_prompt"),
    (re.compile(r'FATAL:|panic:'), "fatal_error"),
    (re.compile(r'Permission denied'), "permission_error"),
    (re.compile(r'No space left on device'), "disk_error"),
]


def wait_for_ready(
    pane_info: PaneInfo,
    marker: str | None = None,
    timeout: float | None = None,
    max_wait: float = MAX_WAIT,
    detect_intervention: bool = False,
) -> tuple[str, str]:
    """
    Wait for a pane command to complete.

    Returns (screen_content, detection_method) where detection_method is
    one of: "marker", "prompt", "idle", "max_wait", "intervention:<type>".

    Uses adaptive polling (10ms → 500ms exponential backoff) for fast
    detection of quick commands while avoiding CPU waste on slow ones.
    """
    idle_timeout = timeout or pane_info.idle_timeout or DEFAULT_IDLE_TIMEOUT
    last_content = ""
    last_change = time.time()
    start = time.time()
    poll_interval = 0.01  # adaptive: start fast (10ms), backoff to 500ms

    while True:
        # Check for cancellation from signal handler
        from executor import _cancel_event
        if _cancel_event.is_set():
            return last_content or "", "cancelled"

        lines = pane_info.pane.cmd("capture-pane", "-p", "-J").stdout
        screen = "\n".join(lines) if lines else ""

        # Strategy 1: unique end marker
        # Guard: the marker also appears in the command echo (send_keys
        # echoes the typed command).  The echo line contains "EXIT:$?" or
        # "EXIT:$_ec" — a dollar sign after "EXIT:".  Real output has
        # "EXIT:<digit>".  Only match lines without "EXIT:$".
        if marker and marker in screen:
            for _line in screen.splitlines():
                if marker in _line and "EXIT:$" not in _line:
                    return screen, "marker"

        # Strategy 2: prompt sentinel on last line
        if lines and "[AGENT_READY] $" in lines[-1]:
            return screen, "prompt"

        # Strategy 2.5: intervention detection (streaming observation)
        if detect_intervention and screen != last_content:
            for pattern, intervention_type in INTERVENTION_PATTERNS:
                if pattern.search(screen):
                    return screen, f"intervention:{intervention_type}"

        # Strategy 3: idle timeout (skip when waiting for a specific marker)
        if screen != last_content:
            last_content = screen
            last_change = time.time()
            poll_interval = 0.01  # reset to fast polling on change
        elif not marker and time.time() - last_change > idle_timeout:
            return screen, "idle"

        # Absolute ceiling
        if time.time() - start > max_wait:
            return screen, "max_wait"

        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 2, 0.5)  # exponential backoff


def wrap_command(
    command: str,
    subtask_id: str,
    done_file: str | None = None,
) -> tuple[str, str]:
    """Wrap a shell command with a unique end marker for reliable detection.

    Args:
        command: The shell command to wrap
        subtask_id: Used to make the marker unique
        done_file: If provided, also write exit code to this file (side-channel).
                   This prevents marker loss when output is very long.

    Returns (wrapped_command, marker_string).
    """
    nonce = uuid.uuid4().hex[:4]
    marker = f"___DONE_{subtask_id}_{nonce}___"
    if done_file:
        # Side-channel: write exit code to file AND echo marker to screen
        wrapped = f'{command}; _ec=$?; echo "$_ec" > {done_file}; echo "EXIT:$_ec {marker}"; (exit $_ec)'
    else:
        # Always capture exit code alongside marker
        wrapped = f'{command}; echo "EXIT:$? {marker}"'
    return wrapped, marker

