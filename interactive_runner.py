"""Interactive-mode subtask execution — read-think-type loop.

Extracted from executor.py. Imports shared primitives from runtime.py
(the leaf module) and direct modules, breaking the former circular
dependency on executor.
"""

import logging
import re
import threading
import time

from command_extract import extract_command, extract_done
from completion import wait_for_ready, wrap_command
from llm import get_client, chat, chat_stream
from streaming_extract import StreamingCommandDetector
from models import Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from prompts import build_interactive_prompt
from remote import render_agent_screen
from runtime import _emit, _check_command_safety, _pane_locks, _cancel_event, _wrap_for_sandbox
from screen_diff import compute_screen_diff
from session import capture_pane

log = logging.getLogger(__name__)


_SHELL_LIKE_APP_TYPES = {"shell", "data", "docs", "media", "browser", "files"}
_EMPTY_REPLY_LIMIT = 2
# Matches the exit code inside the wrap_command marker line: "EXIT:<n> ___DONE_..."
# The "EXIT:$" guard (unexpanded variable in the echoed command) is applied
# before this regex to avoid matching the command echo itself.
_EXIT_CODE_RE = re.compile(r"EXIT:(\d+)")


def _parse_exit_code(screen: str) -> int | None:
    """Extract the exit code from the most recent marker line in a captured screen.

    Returns None if no marker line is found (e.g. timeout before completion).
    Scans bottom-up so the most recent command wins when multiple markers exist.
    """
    for line in reversed(screen.splitlines()):
        if "EXIT:" in line and "EXIT:$" not in line:
            match = _EXIT_CODE_RE.search(line)
            if match:
                return int(match.group(1))
    return None


def _trim_messages(messages: list[dict], max_user_turns: int = 4) -> list[dict]:
    """Trim conversation history to system prompt + first turn + last N turns.

    Bookend strategy: keeps the first user turn (initial screen context —
    working directory, available files) alongside the most recent turns.
    Prevents unbounded growth while preserving critical early context.
    """
    if not messages:
        return messages

    system = [m for m in messages if m["role"] == "system"]
    conversation = [m for m in messages if m["role"] != "system"]

    user_indices = [i for i, m in enumerate(conversation) if m["role"] == "user"]

    if len(user_indices) <= max_user_turns:
        return messages

    # Keep first user-assistant pair (initial context) + last N-1 pairs
    first_pair = conversation[:2] if len(conversation) >= 2 else conversation[:1]
    cutoff_idx = user_indices[-(max_user_turns - 1)] if max_user_turns > 1 else user_indices[-1]
    recent = conversation[cutoff_idx:]

    return system + first_pair + recent


def _send_agent_command(cmd: str, subtask: Subtask, pane_info: PaneInfo, session_dir: str) -> tuple[str, str]:
    """Wrap, sandbox, send, and wait.

    Returns (screen, detection_method) so callers can react to
    "intervention:<type>" states (y/N prompts, password prompts, fatal
    errors) instead of waiting for the 2s idle timeout to trip.
    """
    if pane_info.app_type in _SHELL_LIKE_APP_TYPES:
        cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)
    wrapped, marker = wrap_command(cmd, subtask.id)
    pane_info.pane.send_keys(wrapped, enter=True)
    screen, method = wait_for_ready(pane_info, marker=marker, detect_intervention=True)
    return screen, method


def run_subtask_interactive(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute a subtask via the read-think-type loop.

    The LLM reads the pane screen, outputs a shell command as plain text,
    and the executor types it into the pane. No XML protocol, no side channels.
    The pane scrollback IS the session store.
    """
    client = get_client()
    total_pt = total_ct = 0
    empty_reply_count = 0

    from llm import MODEL
    from runtime import context_budget
    budget = context_budget(MODEL)

    system_prompt = build_interactive_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Begin. Goal: {subtask.description}"},
    ]
    prev_screen = None

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
    with lock:
        for turn in range(1, subtask.max_turns + 1):
            if _cancel_event.is_set():
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary="Cancelled", output_snippet="",
                    turns_used=turn - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            try:
                screen = capture_pane(pane_info)
            except Exception as exc:
                log.exception("capture_pane failed at turn %d", turn)
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary=f"capture_pane crashed: {exc}",
                    output_snippet=prev_screen[-500:] if prev_screen else "",
                    turns_used=turn - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )
            # Delegate side-channel: if the inner is asking the outer
            # for inference (LLM_PROVIDER=delegate), answer it now and
            # loop without consuming an outer-LLM turn. The raw
            # llm_request/response frames live in the pane scrollback;
            # we operate on the raw screen BEFORE rendering for the
            # LLM view below.
            if pane_info.app_type == "agent":
                from executor import handle_agent_pane_frame
                if handle_agent_pane_frame(
                    pane_info.pane, screen, nonce=pane_info.frame_nonce
                ):
                    time.sleep(0.2)
                    continue
            # For agent panes, decode framed protocol messages into
            # human-readable pseudo-lines BEFORE the LLM sees them.
            # The outer LLM never parses raw frames — it reads the
            # decoded view described in drivers/agent.md.
            if pane_info.app_type == "agent":
                screen = render_agent_screen(screen, nonce=pane_info.frame_nonce)
            diff = compute_screen_diff(prev_screen, screen)
            prev_screen = screen

            messages.append({"role": "user", "content": diff})
            messages = _trim_messages(messages, max_user_turns=budget["max_user_turns"])

            early_cmd = []
            detector = StreamingCommandDetector(
                on_command=lambda cmd: early_cmd.append(cmd),
            )
            try:
                reply, pt, ct = chat_stream(client, messages, on_token=detector.feed)
            except Exception:
                try:
                    reply, pt, ct = chat(client, messages)
                except Exception as exc:
                    log.exception("LLM call failed at turn %d", turn)
                    return SubtaskResult(
                        subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                        summary=f"LLM call crashed: {exc}",
                        output_snippet=screen[-500:] if screen else "",
                        turns_used=turn - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                    )
            total_pt += pt
            total_ct += ct

            if not reply.strip():
                empty_reply_count += 1
                if empty_reply_count >= _EMPTY_REPLY_LIMIT:
                    return SubtaskResult(
                        subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                        summary=f"LLM returned {_EMPTY_REPLY_LIMIT} consecutive empty responses",
                        output_snippet=screen[-500:] if screen else "",
                        turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                    )
                continue
            empty_reply_count = 0

            messages.append({"role": "assistant", "content": reply})
            _emit(on_event, "turn", subtask.id, turn, reply[:80])
            _emit(on_event, "tokens", subtask.id, pt, ct)

            done = extract_done(reply)
            if done is not None:
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
                    summary=done, output_snippet=screen[-500:],
                    turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            cmd = extract_command(reply)
            if not cmd:
                continue

            violation = _check_command_safety(cmd)
            if violation:
                log.warning(violation)
                messages.append({"role": "user", "content": f"[BLOCKED] {violation}. Try a different approach."})
                continue

            prev_screen, detection = _send_agent_command(cmd, subtask, pane_info, session_dir)
            # Surface non-zero exit codes explicitly so the LLM can't miss
            # them via the screen diff alone. The marker wraps every command
            # with `echo "EXIT:$? ___DONE_..."`, so the code is always present.
            # NOTE: _parse_exit_code runs BEFORE the agent-pane render so
            # that raw `EXIT:<n>` markers (injected by wrap_command) are
            # still visible — the renderer strips frames, not shell output.
            exit_code = _parse_exit_code(prev_screen)
            # Keep prev_screen in the same form as the decoded screen we
            # will compare against on the next turn, so compute_screen_diff
            # does not report "all frames disappeared" as noise.
            if pane_info.app_type == "agent":
                prev_screen = render_agent_screen(prev_screen, nonce=pane_info.frame_nonce)
            if exit_code is not None and exit_code != 0:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[EXIT:{exit_code}] Previous command exited non-zero. "
                        "Inspect the screen output and try a different approach."
                    ),
                })
            # Intervention detection: if the pane is now sitting at a prompt
            # for human input (y/N, password, "Press any key"...) or a fatal
            # error, tell the LLM explicitly so it can respond on the next
            # turn instead of waiting for the idle timeout to trip.
            if detection.startswith("intervention:"):
                intervention_type = detection.split(":", 1)[1]
                messages.append({
                    "role": "user",
                    "content": (
                        f"[INTERVENTION:{intervention_type}] The pane is waiting "
                        "for input or reporting a fatal condition. Respond "
                        "directly (e.g. type 'y', a password, or abort) — "
                        "do NOT issue a new shell command until this is resolved."
                    ),
                })

    # Exhausted turns
    final_screen = capture_pane(pane_info)
    return SubtaskResult(
        subtask_id=subtask.id, status=SubtaskStatus.FAILED,
        summary=f"Exhausted {subtask.max_turns} turns without completing",
        output_snippet=final_screen[-500:],
        turns_used=subtask.max_turns, prompt_tokens=total_pt, completion_tokens=total_ct,
    )
