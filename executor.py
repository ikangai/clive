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
from interactive_runner import run_subtask_interactive, _trim_messages  # noqa: E402
# DAG scheduler lives in dag_scheduler.py. Re-export the symbols that other
# modules / tests import from executor (execute_plan, _try_collapse_plan,
# _build_plan_context, _build_dependency_context).
from dag_scheduler import (  # noqa: E402
    execute_plan,
    _try_collapse_plan,
    _build_plan_context,
    _build_dependency_context,
)


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
    via CLIVE_FRAME_NONCE. Forged frames carrying any other nonce are
    silently dropped — a compromised LLM inside the inner cannot make
    the outer burn inference tokens by fabricating a request.
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
    messages = req.payload.get("messages", [])
    max_tokens = int(req.payload.get("max_tokens", 1024))
    model = req.payload.get("model")

    try:
        client = llm.get_client()
        content, pt, ct = llm.chat(
            client,
            messages,
            max_tokens=max_tokens,
            # When the inner asked for model="delegate" (its placeholder),
            # let the outer fall back to its own configured model.
            model=model if model and model != "delegate" else None,
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
    cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)
    nonce = _uuid.uuid4().hex[:4]
    marker = f"___DONE_{subtask.id}_{nonce}___"
    out_file = os.path.join(session_dir, f"_direct_{subtask.id}.out")
    ec_file = os.path.join(session_dir, f"_direct_{subtask.id}.ec")

    # Redirect output to file, write exit code to file, then echo marker
    combined = f'{cmd} > {out_file} 2>&1; echo $? > {ec_file}; echo "{marker}"'

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
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

    # Smart max_turns: mode-aware defaults when planner didn't specify
    _MODE_TURNS = {"script": 3, "interactive": 8, "streaming": 10}
    if subtask.max_turns == 15:  # default wasn't overridden
        subtask.max_turns = _MODE_TURNS.get(subtask.mode, 8)

    # Interactive and streaming modes → v2 worker
    return run_subtask_interactive(
        subtask=subtask,
        pane_info=pane_info,
        dep_context=dep_context,
        on_event=on_event,
        session_dir=session_dir,
    )


