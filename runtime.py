"""Shared runtime primitives for the execution layer.

Leaf module — does NOT import from executor, interactive_runner,
script_runner, dag_scheduler, or completion. All those modules import
from here, breaking what was previously a fragile circular dependency.
"""

import logging
import os
import re
import shlex
import threading

log = logging.getLogger(__name__)

# ─── Per-pane locks: only one subtask can use a pane at a time ───────────────
_pane_locks: dict[str, threading.Lock] = {}

# ─── Global cancellation event — set by signal handler to abort all workers ──
_cancel_event = threading.Event()


def cancel():
    """Signal all workers to stop."""
    _cancel_event.set()


def is_cancelled() -> bool:
    """Check if cancellation has been requested."""
    return _cancel_event.is_set()


def reset_cancel():
    """Reset cancellation state for a new run."""
    _cancel_event.clear()


# ─── Event emission ─────────────────────────────────────────────────────────

def _emit(on_event, *args):
    """Call event callback if provided."""
    if on_event:
        try:
            on_event(*args)
        except Exception:
            log.debug("on_event callback failed for %s", args[0] if args else "?", exc_info=True)


# ─── Command Safety ─────────────────────────────────────────────────────────

BLOCKED_COMMANDS = [
    re.compile(r'rm\s+(-\w*\s+)*-r[f ]\s+/\s*$'),
    re.compile(r'rm\s+(-\w*\s+)*-rf\s+(~|\$HOME|/home)\b'),
    re.compile(r'\b(shutdown|reboot|halt|poweroff)\b'),
    re.compile(r'\bmkfs\b'),
    re.compile(r'\bdd\s+.*of=/dev/'),
    re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'),  # fork bomb
    re.compile(r'>\s*/dev/sd[a-z]'),
    re.compile(r'chmod\s+(-\w+\s+)*777\s+/\s*$'),
    re.compile(r'\bwhile\s+true\s*;\s*do\s*:?\s*;?\s*done'),
    re.compile(r'\beval\s+"?\$\(.*base64'),
]


def _check_command_safety(command: str) -> str | None:
    """Check command against blocklist. Returns violation or None."""
    for pattern in BLOCKED_COMMANDS:
        if pattern.search(command):
            return f"Blocked dangerous command: {command[:80]}"
    return None


# ─── Sandbox Wrapping ───────────────────────────────────────────────────────

def _wrap_for_sandbox(cmd: str, session_dir: str, sandboxed: bool = False, no_network: bool = False) -> str:
    """Wrap a command through the sandbox script if sandboxing is enabled."""
    if not sandboxed and os.environ.get("CLIVE_SANDBOX") != "1":
        return cmd
    script = os.path.join(os.path.dirname(__file__), "sandbox", "run.sh")
    parts = ["bash", shlex.quote(script), shlex.quote(session_dir)]
    if no_network:
        parts.append("--no-network")
    parts.append(shlex.quote(cmd))
    return " ".join(parts)


# ─── File Writing ───────────────────────────────────────────────────────────

def write_file(path: str, content: str) -> str:
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"[Written: {path}]"
    except Exception as e:
        return f"[Error writing {path}: {e}]"


# ─── Script Extraction ─────────────────────────────────────────────────────

def _extract_script(text: str) -> str:
    """Extract bash or Python script from LLM response."""
    # Try fenced code block (bash, sh, or python)
    m = re.search(r'```(?:bash|sh|python[3]?)?\s*\n([\s\S]*?)```', text)
    if m:
        return m.group(1).strip()
    # Try unfenced: everything from shebang to end (or next ```)
    m = re.search(r'(#!(?:/bin/bash|/usr/bin/env python[3]?)[\s\S]*?)(?:```|$)', text)
    if m:
        return m.group(1).strip()
    raise ValueError(f"No script found in response:\n{text[:200]}")
