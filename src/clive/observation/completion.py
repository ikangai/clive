"""Completion and intervention detection for tmux pane commands.

Completion strategies (checked in priority order):
1. Unique end marker (for shell commands)
2. Prompt sentinel: [AGENT_READY] $ on last line
3. Idle timeout: screen unchanged for N seconds

Intervention detection (for streaming observation level):
Scans screen for patterns that indicate the agent should intervene
(prompts for input, errors, confirmations). Returns early with
method="intervention" when detected.

Event-driven path (streaming observation pipeline):
When an asyncio.Queue of ByteEvents is provided, `await_ready_events`
blocks on those events instead of polling `capture-pane`. See
`await_ready_events` and `wait_for_ready(event_source=...)`.
"""

import asyncio
import re
import time
import uuid
from typing import Optional

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

# Map ByteEvent kinds (from observation.byte_classifier.BYTE_PATTERNS) to the
# intervention:<type> strings produced by the poll path. Only kinds the L2
# byte classifier can actually emit appear here — "fatal_error", "disk_error",
# "overwrite_prompt", "continue_prompt" are screen-regex-only today.
_INTERVENTION_KIND_MAP = {
    "password_prompt": "password_prompt",
    "confirm_prompt":  "confirmation_prompt",
    "permission_error": "permission_error",
}


def wait_for_ready(
    pane_info: PaneInfo,
    marker: str | None = None,
    timeout: float | None = None,
    max_wait: float = MAX_WAIT,
    detect_intervention: bool = False,
    event_source: "Optional[asyncio.Queue]" = None,
) -> tuple[str, str]:
    """
    Wait for a pane command to complete.

    Returns (screen_content, detection_method) where detection_method is
    one of: "marker", "prompt", "idle", "max_wait", "intervention:<type>".

    When `event_source` is None (default), uses adaptive polling
    (10ms -> 500ms exponential backoff) of `capture-pane`.

    When `event_source` is provided, consumes ByteEvents from the queue
    instead. This requires an asyncio loop; if called from a thread with
    no running loop, a temporary loop is spun up. If called from inside a
    running loop, raises RuntimeError — callers in async contexts should
    `await await_ready_events(...)` directly.
    """
    if event_source is None:
        return _wait_polling(
            pane_info, marker, timeout, max_wait, detect_intervention,
        )

    # Event-driven path needs an asyncio loop. If a loop is already
    # running in this thread, blocking on run_until_complete would
    # deadlock — force the caller to use the async entry point.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread — safe to spin a temp one.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(await_ready_events(
                pane_info,
                event_source,
                marker=marker,
                timeout=timeout,
                max_wait=max_wait,
                detect_intervention=detect_intervention,
            ))
        finally:
            loop.close()
    raise RuntimeError(
        "wait_for_ready(event_source=...) called from a running loop; "
        "use 'await await_ready_events(...)' directly instead."
    )


def _wait_polling(
    pane_info: PaneInfo,
    marker: str | None,
    timeout: float | None,
    max_wait: float,
    detect_intervention: bool,
) -> tuple[str, str]:
    """Poll-path implementation of wait_for_ready. Unchanged behavior."""
    idle_timeout = timeout or pane_info.idle_timeout or DEFAULT_IDLE_TIMEOUT
    last_content = ""
    last_change = time.time()
    start = time.time()
    poll_interval = 0.01  # adaptive: start fast (10ms), backoff to 500ms

    while True:
        # Check for cancellation from signal handler
        from runtime import _cancel_event
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


async def await_ready_events(
    pane_info: PaneInfo,
    event_source: asyncio.Queue,
    marker: str | None = None,
    timeout: float | None = None,
    max_wait: float = MAX_WAIT,
    detect_intervention: bool = False,
) -> tuple[str, str]:
    """Async, event-driven counterpart to `wait_for_ready`.

    Blocks on ByteEvents delivered via `event_source` (typically a
    PaneStream subscriber queue) instead of polling `capture-pane`.
    Returns `(screen_content, detection_method)` with the same
    `detection_method` vocabulary as the poll path:
    "marker", "idle", "max_wait", "intervention:<type>".

    Note: "prompt" (the [AGENT_READY] $ sentinel) is poll-only — the
    byte classifier does not emit a dedicated event for it today.
    Callers that need prompt-sentinel detection should stay on the
    poll path or add a classifier pattern.
    """
    idle = timeout or pane_info.idle_timeout or DEFAULT_IDLE_TIMEOUT
    start = time.time()
    method = "max_wait"

    while True:
        remaining = max_wait - (time.time() - start)
        if remaining <= 0:
            method = "max_wait"
            break

        # Cap per-event wait at the smaller of idle and remaining — we
        # want to treat "no events for idle seconds" as idle even if
        # max_wait hasn't fired yet.
        wait_slice = min(idle, remaining)
        try:
            evt = await asyncio.wait_for(event_source.get(), timeout=wait_slice)
        except asyncio.TimeoutError:
            # If max_wait already elapsed, the next loop iteration would
            # return max_wait; prefer idle when there's headroom.
            if max_wait - (time.time() - start) <= 0:
                method = "max_wait"
            else:
                method = "idle"
            break

        # cmd_end with marker match -> "marker"
        if marker and evt.kind == "cmd_end":
            if marker.encode() in evt.match_bytes:
                method = "marker"
                break

        # intervention mapping
        if detect_intervention and evt.kind in _INTERVENTION_KIND_MAP:
            method = f"intervention:{_INTERVENTION_KIND_MAP[evt.kind]}"
            break

        # Otherwise keep waiting — non-target events don't stop us.

    # Final screen capture (same shape as poll path).
    lines = pane_info.pane.cmd("capture-pane", "-p", "-J").stdout
    screen = "\n".join(lines) if lines else ""
    return screen, method


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
