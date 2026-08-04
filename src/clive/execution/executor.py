"""Execution facade — mode dispatcher, direct-mode worker, delegate handler.

Re-exports symbols from runtime, dag_scheduler, script_runner, and
interactive_runner so consumers can ``from executor import execute_plan``
without knowing the internal module structure.
"""

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

from models import Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from completion import wait_for_ready

# Shared runtime primitives — canonical definitions live in runtime.py.
# Re-exported here for backward compatibility (existing tests and modules
# that `from executor import ...` or access `executor.X`).
from runtime import (  # noqa: E402
    _pane_locks,
    _cancel_event,
    cancel,
    is_cancelled,
    reset_cancel,
    _emit,
    BLOCKED_COMMANDS,
    _check_command_safety,
    _wrap_for_sandbox,
    write_file,
    _extract_script,
)

# Script- and interactive-mode workers live in sibling modules. Re-exported
# here so `executor.run_subtask_script` / `run_subtask_interactive` still
# resolve (tests patch them at these names).
from script_runner import run_subtask_script  # noqa: E402
from interactive_runner import run_subtask_interactive, _trim_messages, _SHELL_LIKE_APP_TYPES  # noqa: E402
from planned_runner import run_subtask_planned  # noqa: E402
from pane_isolation import isolation_enabled  # noqa: E402
from llm_runner import run_subtask_llm  # noqa: E402
# DAG scheduler lives in dag_scheduler.py. Re-export the symbols that other
# modules / tests import from executor (execute_plan, _try_collapse_plan,
# _build_plan_context, _build_dependency_context).
from dag_scheduler import (  # noqa: E402
    execute_plan,
    _try_collapse_plan,
    _build_plan_context,
    _build_dependency_context,
)


# Delegate request limits (gh#47). The remote authors this payload, so it
# is untrusted input even on an authenticated channel.
DELEGATE_MAX_TOKENS_CEILING = 8192
DELEGATE_MAX_MESSAGES = 200
DELEGATE_MAX_CONTENT_CHARS = 200_000
# `system` is excluded deliberately: llm.chat lifts a system-role message
# into the provider's top-level system parameter, so accepting one would
# hand the remote control of the outer's system prompt.
DELEGATE_ALLOWED_ROLES = {"user", "assistant"}


def _clamp_delegate_max_tokens(raw) -> int:
    """Coerce and bound a remote-supplied max_tokens.

    Uncapped, the remote picks how much the outer spends per request;
    uncoerced, a non-numeric value raises out of the handler and kills
    the outer's subtask.
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1024
    if n <= 0:
        return 1024
    return min(n, DELEGATE_MAX_TOKENS_CEILING)


def _validate_delegate_messages(raw) -> list:
    """Return a sanitized message list, or raise ValueError.

    Rejects anything that is not a list of {role, content} dicts with an
    allowed role, and bounds both the count and the total size.
    """
    if not isinstance(raw, list):
        raise ValueError("delegate request: messages must be a list")
    if not raw:
        raise ValueError("delegate request: messages must not be empty")
    if len(raw) > DELEGATE_MAX_MESSAGES:
        raise ValueError(
            f"delegate request: too many messages ({len(raw)} > "
            f"{DELEGATE_MAX_MESSAGES})"
        )
    total = 0
    clean = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ValueError(f"delegate request: message {i} is not an object")
        role = m.get("role")
        content = m.get("content")
        if role not in DELEGATE_ALLOWED_ROLES:
            raise ValueError(
                f"delegate request: message {i} has disallowed role {role!r} "
                f"(allowed: {sorted(DELEGATE_ALLOWED_ROLES)})"
            )
        if not isinstance(content, str):
            raise ValueError(
                f"delegate request: message {i} content must be a string"
            )
        total += len(content)
        if total > DELEGATE_MAX_CONTENT_CHARS:
            raise ValueError(
                f"delegate request: content exceeds "
                f"{DELEGATE_MAX_CONTENT_CHARS} chars"
            )
        # Drop any extra keys the remote tacked on rather than forwarding
        # them into the provider call.
        clean.append({"role": role, "content": content})
    return clean


def handle_agent_pane_frame(pane, screen_blob: str, nonce: str) -> bool:
    """Answer an unanswered llm_request frame on an agent pane.

    When the inner clive is configured with LLM_PROVIDER=delegate, it
    serializes each inference call as an llm_request frame. The outer
    pane reader detects the frame, calls its own local llm.chat() with
    the forwarded messages, and types back an llm_response (or
    llm_error) frame via tmux send_keys. This is a side-channel round
    trip — the caller MUST NOT advance its turn state when this
    returns True.

    Returns True iff a delegate request was handled (the outer's loop
    should sleep briefly and continue without consuming a turn).

    ``nonce`` is the session nonce the outer injected into the inner
    via CLIVE_FRAME_NONCE. Frames carrying any other nonce — or none at
    all — are dropped by decode_all (gh#46).

    The frame payload is still remote-authored input even when the
    channel authenticates, so every field is validated before it
    reaches the outer's paid client (gh#47): messages must be
    well-formed and may not carry a `system` role (llm.chat lifts that
    into the provider's top-level system parameter, i.e. it would let
    the remote set the outer's system prompt), max_tokens is clamped,
    and the remote does not get to choose the outer's model.
    """
    from protocol import decode_all, encode, latest
    import llm

    frames = decode_all(screen_blob, nonce=nonce)
    req = latest(frames, "llm_request")
    if req is None:
        return False

    # Dedup: if a response frame with the same id already exists, we
    # have already answered this request on a previous poll.
    resp = latest(frames, "llm_response")
    if resp is not None and resp.payload.get("id") == req.payload.get("id"):
        return False

    rid = req.payload.get("id", "unknown")

    try:
        messages = _validate_delegate_messages(req.payload.get("messages"))
        max_tokens = _clamp_delegate_max_tokens(req.payload.get("max_tokens"))
        client = llm.get_client()
        content, pt, ct = llm.chat(
            client,
            messages,
            max_tokens=max_tokens,
            # The remote does not choose the outer's model — that would let
            # it pick the outer's tier and therefore the outer's bill. Always
            # use the outer's own configured model.
            model=None,
        )
        out = encode("llm_response", {
            "id": rid,
            "content": content,
            "prompt_tokens": pt,
            "completion_tokens": ct,
        }, nonce=nonce)
    except Exception as e:
        log.exception("delegate llm call failed for id=%s", rid)
        out = encode("llm_error", {"id": rid, "error": str(e)}, nonce=nonce)

    pane.send_keys(out, enter=True)
    return True



# ─── Direct Mode Worker ──────────────────────────────────────────────────────

def _wait_for_ec_file(ec_file: str, max_wait: float = 30.0, poll: float = 0.1) -> bool:
    """Wait until the exit-code file exists and is non-empty.

    The shell writes ``echo $? > ec_file`` after the command completes,
    so a non-empty file means the command (and its redirect) finished.
    Lock-free — concurrent direct subtasks on the same pane each poll
    their own file (gh#14).
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            if os.path.getsize(ec_file) > 0:
                return True
        except OSError:
            pass
        time.sleep(poll)
    return False


def run_subtask_direct(
    subtask: Subtask,
    pane_info: PaneInfo,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Run a literal shell command directly — zero LLM calls.

    Uses file-based output capture instead of screen scraping to handle
    commands with large output (e.g. curl) that would scroll the marker
    off the visible tmux pane.
    """
    import uuid as _uuid

    cmd = subtask.description.strip()

    # Safety gate parity (Audit C1, 2026-05-27). The Tier-1 classifier can
    # emit a `direct` subtask with an LLM-chosen cmd; without this check the
    # gate that protects interactive/toolcall/planned modes is bypassed.
    violation = _check_command_safety(cmd)
    if violation:
        log.warning(f"[{subtask.id}] Direct cmd blocked: {violation}")
        return SubtaskResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary=f"Blocked by safety gate: {violation}",
            output_snippet="",
            turns_used=0,
            exit_code=None,
        )

    cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)
    nonce = _uuid.uuid4().hex[:4]
    marker = f"___DONE_{subtask.id}_{nonce}___"
    out_file = os.path.join(session_dir, f"_direct_{subtask.id}.out")
    ec_file = os.path.join(session_dir, f"_direct_{subtask.id}.ec")

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())

    if isolation_enabled() and pane_info.app_type in _SHELL_LIKE_APP_TYPES:
        # gh#14 response isolation: output already goes to per-subtask
        # files, so the lock only needs to cover keystroke delivery. The
        # subshell stops env/cwd leakage between subtasks sharing the
        # pane; waiting on our own ec_file is concurrent — queued
        # commands type-ahead in the tty and run sequentially.
        combined = f'( {cmd} ) > {out_file} 2>&1; echo $? > {ec_file}; echo "{marker}"'
        with lock:
            pane_info.pane.send_keys(combined, enter=True)
        _wait_for_ec_file(ec_file, max_wait=30.0)
    else:
        # Redirect output to file, write exit code to file, then echo marker
        combined = f'{cmd} > {out_file} 2>&1; echo $? > {ec_file}; echo "{marker}"'
        with lock:
            pane_info.pane.send_keys(combined, enter=True)
            time.sleep(0.15)  # Let shell start processing before polling
            wait_for_ready(pane_info, marker=marker, max_wait=30.0)

    # Read exit code from file
    exit_code = 0
    try:
        with open(ec_file) as f:
            exit_code = int(f.read().strip())
    except (OSError, ValueError):
        pass

    # Read output from file
    output = ""
    try:
        with open(out_file, errors="replace") as f:
            output = f.read()
    except OSError:
        pass

    summary = output.strip()[-2000:] if output.strip() else "Done (no output)"
    status = SubtaskStatus.COMPLETED if exit_code == 0 else SubtaskStatus.FAILED

    return SubtaskResult(
        subtask_id=subtask.id,
        status=status,
        summary=summary,
        output_snippet=output[-500:] if len(output) > 500 else output,
        turns_used=1,
        exit_code=exit_code,
    )



# ─── Per-Subtask Worker ───────────────────────────────────────────────────────

def run_subtask(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
    token_budget: int = 50000,
) -> SubtaskResult:
    """Execute a single subtask. Dispatches based on observation level (mode)."""
    # Check for executable skill: if description contains [skill:name] and the skill
    # has STEPS, run mechanically (zero LLM calls on happy path)
    import re as _re
    _skill_match = _re.search(r'\[skill:([\w-]+(?:\s+\w+=\S+)*)\]', subtask.description)
    if _skill_match:
        from skills import load_skill, resolve_skill_with_params, inject_params
        from skill_runner import parse_executable_steps, run_executable_skill
        skill_ref = _skill_match.group(1)
        skill_name, params = resolve_skill_with_params(skill_ref)
        skill_content = load_skill(skill_name)
        if skill_content:
            skill_content = inject_params(skill_content, params)
            steps = parse_executable_steps(skill_content)
            if steps:
                logging.debug(f"[{subtask.id}] Executable skill: {skill_name} ({len(steps)} steps)")
                return run_executable_skill(
                    steps=steps,
                    pane_info=pane_info,
                    session_dir=session_dir,
                    params=params,
                    subtask_id=subtask.id,
                )

    if subtask.mode == "direct":
        return run_subtask_direct(
            subtask=subtask,
            pane_info=pane_info,
            on_event=on_event,
            session_dir=session_dir,
        )

    if subtask.mode == "script":
        return run_subtask_script(
            subtask=subtask,
            pane_info=pane_info,
            dep_context=dep_context,
            on_event=on_event,
            session_dir=session_dir,
        )

    if subtask.mode == "planned":
        return run_subtask_planned(
            subtask=subtask,
            pane_info=pane_info,
            dep_context=dep_context,
            on_event=on_event,
            session_dir=session_dir,
        )

    if subtask.mode == "llm":
        return run_subtask_llm(
            subtask=subtask,
            pane_info=pane_info,
            dep_context=dep_context,
            on_event=on_event,
            session_dir=session_dir,
        )

    # Smart max_turns: mode-aware defaults when planner didn't specify
    _MODE_TURNS = {"script": 3, "interactive": 8, "streaming": 10}
    if subtask.max_turns == 15:  # default wasn't overridden
        subtask.max_turns = _MODE_TURNS.get(subtask.mode, 8)

    # Interactive and streaming modes — try tool-calling runner first
    if subtask.mode in ("interactive", "streaming"):
        from llm import PROVIDER_NAME
        _toolcall_providers = {"openai", "anthropic", "openrouter", "gemini"}
        if PROVIDER_NAME in _toolcall_providers:
            try:
                from toolcall_runner import run_subtask_toolcall
                return run_subtask_toolcall(
                    subtask=subtask, pane_info=pane_info, dep_context=dep_context,
                    on_event=on_event, session_dir=session_dir,
                )
            except Exception:
                log.debug("Tool-calling runner failed, falling back to text-based", exc_info=True)

    # Fallback: text-based interactive runner
    return run_subtask_interactive(
        subtask=subtask,
        pane_info=pane_info,
        dep_context=dep_context,
        on_event=on_event,
        session_dir=session_dir,
        token_budget=token_budget,
    )


