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

    If detect_intervention is True, also scans for patterns that indicate
    the agent should intervene (prompts, errors, confirmations).
    """
    idle_timeout = timeout or pane_info.idle_timeout or DEFAULT_IDLE_TIMEOUT
    last_content = ""
    last_change = time.time()
    start = time.time()

    while True:
        lines = pane_info.pane.cmd("capture-pane", "-p").stdout
        screen = "\n".join(lines) if lines else ""

        # Strategy 1: unique end marker
        if marker and marker in screen:
            return screen, "marker"

        # Strategy 2: prompt sentinel on last line
        if lines and "[AGENT_READY] $" in lines[-1]:
            return screen, "prompt"

        # Strategy 2.5: intervention detection (streaming observation)
        if detect_intervention and screen != last_content:
            # Only check new content for intervention patterns
            for pattern, intervention_type in INTERVENTION_PATTERNS:
                if pattern.search(screen):
                    return screen, f"intervention:{intervention_type}"

        # Strategy 3: idle timeout
        if screen != last_content:
            last_content = screen
            last_change = time.time()
        elif time.time() - last_change > idle_timeout:
            return screen, "idle"

        # Absolute ceiling
        if time.time() - start > max_wait:
            return screen, "max_wait"

        time.sleep(0.1)


def wrap_command(command: str, subtask_id: str) -> tuple[str, str]:
    """Wrap a shell command with a unique end marker for reliable detection.

    Returns (wrapped_command, marker_string).
    """
    nonce = uuid.uuid4().hex[:4]
    marker = f"___DONE_{subtask_id}_{nonce}___"
    wrapped = f'{command}; echo "{marker}"'
    return wrapped, marker
