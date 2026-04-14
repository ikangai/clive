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
import base64
import binascii
import json
import logging
import os
import subprocess

from output import progress
from protocol import KINDS, decode_all, latest, _FRAME_RE  # type: ignore[attr-defined]

_log = logging.getLogger(__name__)

# Kinds suppressed from the LLM-facing decoded view.
# `alive` is keepalive/supervisor signal.
# `session_hello` and the member→lobby "intent" frames (join_room, list_threads,
# open_thread, close_thread, join_thread, leave_thread) are outbound-only from
# a member's perspective; echoing them into that member's LLM context is noise.
# Lobby→member responses (session_ack, threads, thread_opened, your_turn, nack)
# and fanout frames (say, pass) DO render because the LLM needs to see them.
_SUPPRESSED_KINDS = frozenset({
    "alive", "session_hello",
    "join_room", "list_threads", "open_thread",
    "close_thread", "join_thread", "leave_thread",
})
_RENDERED_KINDS = frozenset(KINDS - _SUPPRESSED_KINDS)


def parse_turn_state(screen: str, nonce: str = "") -> str | None:
    """Parse the latest turn state from framed screen content.

    Returns "thinking", "waiting", "done", "failed", or None.

    ``nonce`` must be the session nonce the outer injected into the
    inner whose pane is being read. Frames carrying any other nonce
    are silently dropped. Default ``""`` means "accept only
    unauthenticated frames" — suitable for tests and the pre-Phase-2
    transition, not for production pane readers.

    None is returned both when no turn frame exists and when the latest
    turn frame's state field is missing or not a string; the malformed
    case logs a warning so it is visible in debug output.
    """
    frame = latest(decode_all(screen, nonce=nonce), "turn")
    if frame is None:
        return None
    state = frame.payload.get("state")
    if not isinstance(state, str):
        _log.warning("turn frame with non-string state: %r", state)
        return None
    return state.lower()


def parse_question(screen: str, nonce: str = "") -> str | None:
    """Parse the latest question from framed screen content.

    Returns the question text, or None if no question frame is found
    or the question text is missing/empty. Non-string text is logged
    as a warning to surface protocol misuse. See parse_turn_state for
    ``nonce`` semantics.
    """
    frame = latest(decode_all(screen, nonce=nonce), "question")
    if frame is None:
        return None
    text = frame.payload.get("text")
    if text is None:
        return None
    if not isinstance(text, str):
        _log.warning("question frame with non-string text: %r", text)
        return None
    if not text.strip():
        return None
    return text


def parse_context(screen: str, nonce: str = "") -> dict | None:
    """Parse the latest context payload from framed screen content.

    See parse_turn_state for ``nonce`` semantics.
    """
    frame = latest(decode_all(screen, nonce=nonce), "context")
    return frame.payload if frame is not None else None


def parse_remote_files(screen: str, nonce: str = "") -> list[str]:
    """Parse all file declarations in order of appearance.

    Semantics: cumulative. Every call returns EVERY file frame present
    in the supplied screen, in the order they appear. Callers polling
    the same pane repeatedly must deduplicate (e.g. track a "seen" set)
    to avoid re-processing files on every poll.

    See parse_turn_state for ``nonce`` semantics.
    """
    out = []
    for f in decode_all(screen, nonce=nonce):
        if f.kind == "file":
            name = f.payload.get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def parse_remote_progress(screen: str, nonce: str = "") -> list[str]:
    """Parse all progress declarations in order of appearance.

    Semantics: cumulative — see parse_remote_files for caller-side
    deduplication guidance. See parse_turn_state for ``nonce`` semantics.
    """
    out = []
    for f in decode_all(screen, nonce=nonce):
        if f.kind == "progress":
            text = f.payload.get("text")
            if isinstance(text, str):
                out.append(text)
    return out


# ─── Outer-side pane rendering ───────────────────────────────────────────────

def _render_frame(kind: str, payload: dict) -> str:
    """Format a single decoded frame as a human-readable pseudo-line.

    The ``⎇ CLIVE»`` prefix is distinctive enough that the outer LLM
    can be told to key on it when reading an agent pane. Shell output
    cannot contain that exact sequence by accident.
    """
    if kind == "turn":
        state = payload.get("state", "?")
        return f"⎇ CLIVE» turn={state}"
    if kind == "question":
        text = payload.get("text", "")
        return f'⎇ CLIVE» question: "{text}"'
    if kind == "context":
        return f"⎇ CLIVE» context: {json.dumps(payload, separators=(',', ':'))}"
    if kind == "file":
        name = payload.get("name", "?")
        return f"⎇ CLIVE» file: {name}"
    if kind == "progress":
        text = payload.get("text", "")
        return f"⎇ CLIVE» progress: {text}"
    if kind in ("llm_request", "llm_response", "llm_error"):
        # Delegation side-channel frames are handled by the executor's
        # inference handler, not the LLM's reasoning loop. Render them
        # terse so a human debugging a pane can see they happened,
        # without cluttering the LLM's context with raw message bodies.
        rid = payload.get("id", "?")
        return f"⎇ CLIVE» {kind} id={rid}"
    # ── Rooms / lobby frames ────────────────────────────────────────────
    if kind == "say":
        author = payload.get("from", "?")
        tid = payload.get("thread_id", "?")
        body = payload.get("body", "")
        return f"⎇ CLIVE» say from {author} [thread {tid}]: {body}"
    if kind == "pass":
        author = payload.get("from", "?")
        tid = payload.get("thread_id", "?")
        return f"⎇ CLIVE» pass from {author} [thread {tid}]"
    if kind == "your_turn":
        # Deliberately do NOT dump `recent` / `summary` — the room_runner
        # consumes the structured payload directly. Rendering them here
        # would give the LLM the same content twice.
        tid = payload.get("thread_id", "?")
        idx = payload.get("message_index", "?")
        return f"⎇ CLIVE» your_turn [thread {tid} msg {idx}]"
    if kind == "thread_opened":
        tid = payload.get("thread_id", "?")
        return f"⎇ CLIVE» thread_opened: {tid}"
    if kind == "nack":
        reason = payload.get("reason", "?")
        ref = payload.get("ref_kind", "?")
        return f"⎇ CLIVE» nack: {reason} (ref: {ref})"
    if kind == "session_ack":
        name = payload.get("name", "?")
        accepted = payload.get("accepted", False)
        reason = payload.get("reason")
        tail = f" reason={reason}" if reason else ""
        return f"⎇ CLIVE» session_ack name={name} accepted={accepted}{tail}"
    if kind == "threads":
        room = payload.get("room", "?")
        count = len(payload.get("threads", []))
        return f"⎇ CLIVE» threads [room {room}]: {count} visible"
    # Fallback for any future kind: render kind + compact payload
    return f"⎇ CLIVE» {kind}: {json.dumps(payload, separators=(',', ':'))}"


def render_agent_screen(screen: str, nonce: str) -> str:
    """Transform an agent pane screen for LLM consumption.

    Replaces every valid framed message (matching ``nonce``) with a
    human-readable pseudo-line. Non-frame content (shell prompts, tool
    output, error messages) is preserved verbatim. Frames with a wrong
    or missing nonce are silently dropped — they are removed from the
    output entirely, not rendered, because they come from an
    untrusted source (a compromised inner's LLM, stray tool output, or
    a replay attack).

    The renderer is a thin boundary layer between the pane and the
    outer LLM's reasoning loop. The LLM reads the returned string via
    ``drivers/agent.md`` which describes the ``⎇ CLIVE»`` pseudo-line
    grammar.
    """
    if not screen:
        return ""

    def _sub(m):
        kind = m.group("kind")
        frame_nonce = m.group("nonce")
        b64 = m.group("b64")
        if kind not in KINDS:
            return ""  # unknown kind — drop
        if frame_nonce != nonce:
            _log.debug("render_agent_screen dropping %s frame: nonce mismatch",
                       kind)
            return ""  # forged / replay — drop
        if kind not in _RENDERED_KINDS:
            return ""  # suppressed for LLM (e.g. alive keepalives)
        try:
            raw = base64.b64decode(b64, validate=True)
            payload = json.loads(raw.decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError,
                json.JSONDecodeError):
            return ""  # malformed — drop
        if not isinstance(payload, dict):
            return ""
        return _render_frame(kind, payload)

    return _FRAME_RE.sub(_sub, screen)


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
