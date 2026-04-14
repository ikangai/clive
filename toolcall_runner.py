"""Tool-calling interactive runner — native tool calls instead of text extraction.

Alternative to interactive_runner.py. Uses the model's native tool-calling
ability (run_command, read_screen, complete) instead of regex-based command
extraction from free text. Enables command batching (multiple commands per
LLM response) and clean separation of reasoning (text) and action (tool calls).
"""

import logging
import re
import threading
import time

import anthropic as _anth

from command_extract import extract_done
from completion import wait_for_ready, wrap_command
from context_compress import compress_context, make_llm_compressor
from interactive_runner import _parse_exit_code, _SHELL_LIKE_APP_TYPES
from llm import get_client, chat_with_tools
from models import Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from observation import ScreenClassifier, format_event_for_llm
from prompts import build_interactive_prompt
from runtime import _emit, _check_command_safety, _pane_locks, _cancel_event, _wrap_for_sandbox, context_budget
from session import capture_pane
from tool_defs import PANE_TOOLS, parse_tool_calls

log = logging.getLogger(__name__)

_EMPTY_REPLY_LIMIT = 2


def _execute_tool_call(tool_call, subtask, pane_info, session_dir):
    """Execute one tool call. Returns result dict.

    Returns {"type": "complete"|"screen"|"command_result"|"error", ...}.
    """
    name = tool_call["name"]
    args = tool_call["args"]

    if name == "complete":
        summary = args.get("summary", "Task completed")
        return {"type": "complete", "summary": summary}

    if name == "read_screen":
        lines = args.get("lines", 50)
        try:
            screen = capture_pane(pane_info, scrollback=lines)
        except Exception as exc:
            return {"type": "error", "message": f"capture_pane failed: {exc}"}
        return {"type": "screen", "content": screen}

    if name == "run_command":
        cmd = args.get("command", "")
        if not cmd.strip():
            return {"type": "error", "message": "Empty command"}

        # Safety check
        violation = _check_command_safety(cmd)
        if violation:
            log.warning(violation)
            return {"type": "error", "message": f"[BLOCKED] {violation}. Try a different approach."}

        # Sandbox wrapping
        if pane_info.app_type in _SHELL_LIKE_APP_TYPES:
            cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)

        # Wrap, send, wait
        wrapped, marker = wrap_command(cmd, subtask.id)
        pane_info.pane.send_keys(wrapped, enter=True)
        screen, detection = wait_for_ready(pane_info, marker=marker, detect_intervention=True)

        exit_code = _parse_exit_code(screen)

        result = {
            "type": "command_result",
            "screen": screen,
            "exit_code": exit_code,
            "detection": detection,
        }
        return result

    return {"type": "error", "message": f"Unknown tool: {name}"}


def run_subtask_toolcall(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute a subtask via tool-calling loop.

    The LLM uses native tool calls (run_command, read_screen, complete)
    instead of text-based command extraction. Supports command batching —
    multiple tool calls per LLM response.
    """
    client = get_client()
    total_pt = total_ct = 0
    empty_reply_count = 0

    from llm import MODEL
    effective_model = pane_info.agent_model or MODEL
    budget = context_budget(effective_model)

    # Build compressor for context management
    _obs_model = pane_info.observation_model
    _compressor = make_llm_compressor(client, model=_obs_model) if _obs_model else None

    # Detect tool-call format based on client type
    fmt = "anthropic" if isinstance(client, _anth.Anthropic) else "openai"

    system_prompt = build_interactive_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )

    # Capture initial screen
    try:
        initial_screen = capture_pane(pane_info)
    except Exception as exc:
        return SubtaskResult(
            subtask_id=subtask.id, status=SubtaskStatus.FAILED,
            summary=f"Initial capture_pane failed: {exc}",
            output_snippet="", turns_used=0,
            prompt_tokens=0, completion_tokens=0,
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Begin. Goal: {subtask.description}\n\nCurrent screen:\n{initial_screen}"},
    ]
    prev_screen = initial_screen
    obs_classifier = ScreenClassifier()

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
    with lock:
        for turn in range(1, subtask.max_turns + 1):
            if _cancel_event.is_set():
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary="Cancelled", output_snippet="",
                    turns_used=turn - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            messages = compress_context(messages, max_user_turns=budget["max_user_turns"], compress_fn=_compressor)

            try:
                tool_calls_raw, text, pt, ct = chat_with_tools(
                    client, messages, PANE_TOOLS, model=effective_model,
                )
            except Exception as exc:
                log.exception("chat_with_tools failed at turn %d", turn)
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary=f"LLM call crashed: {exc}",
                    output_snippet=prev_screen[-500:] if prev_screen else "",
                    turns_used=turn - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            total_pt += pt
            total_ct += ct

            # Parse tool calls
            calls = parse_tool_calls(tool_calls_raw, format=fmt)

            _emit(on_event, "turn", subtask.id, turn, text[:80] if text else "(tool calls)")
            _emit(on_event, "tokens", subtask.id, pt, ct)

            # No tool calls — check for text-based DONE fallback
            if not calls:
                if not text or not text.strip():
                    empty_reply_count += 1
                    if empty_reply_count >= _EMPTY_REPLY_LIMIT:
                        return SubtaskResult(
                            subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                            summary=f"LLM returned {_EMPTY_REPLY_LIMIT} consecutive empty responses",
                            output_snippet=prev_screen[-500:] if prev_screen else "",
                            turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                        )
                    messages.append({"role": "assistant", "content": text or ""})
                    continue

                empty_reply_count = 0
                messages.append({"role": "assistant", "content": text})

                # Text-based DONE fallback
                done = extract_done(text)
                if done is not None:
                    return SubtaskResult(
                        subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
                        summary=done, output_snippet=prev_screen[-500:] if prev_screen else "",
                        turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                    )
                # No tool calls and no DONE — add screen and continue
                try:
                    screen = capture_pane(pane_info)
                    prev_screen = screen
                except Exception:
                    pass
                messages.append({"role": "user", "content": f"Current screen:\n{prev_screen}"})
                continue

            empty_reply_count = 0

            # Append assistant message with text (for conversation tracking)
            if text:
                messages.append({"role": "assistant", "content": text})

            # Execute each tool call
            results_text = []
            for tc in calls:
                result = _execute_tool_call(tc, subtask, pane_info, session_dir)

                if result["type"] == "complete":
                    return SubtaskResult(
                        subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
                        summary=result["summary"],
                        output_snippet=prev_screen[-500:] if prev_screen else "",
                        turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                    )

                if result["type"] == "screen":
                    results_text.append(f"[read_screen]\n{result['content']}")

                elif result["type"] == "error":
                    results_text.append(f"[error] {result['message']}")

                elif result["type"] == "command_result":
                    screen = result["screen"]
                    exit_code = result["exit_code"]
                    detection = result["detection"]
                    prev_screen = screen

                    # Build result description
                    parts = []
                    if exit_code is not None and exit_code != 0:
                        parts.append(f"[EXIT:{exit_code}] Command exited non-zero.")
                    elif exit_code == 0:
                        # Observation classification for successful commands
                        obs_event = obs_classifier.classify(screen, exit_code=exit_code)
                        if not obs_event.needs_llm:
                            parts.append(format_event_for_llm(obs_event))

                    if detection and detection.startswith("intervention:"):
                        intervention_type = detection.split(":", 1)[1]
                        parts.append(
                            f"[INTERVENTION:{intervention_type}] The pane is waiting "
                            "for input or reporting a fatal condition."
                        )

                    if not parts:
                        # Show screen tail for context
                        parts.append(f"Screen after command:\n{screen[-500:]}")

                    results_text.append("\n".join(parts))

            # Append all tool results as user message for next turn
            combined_results = "\n---\n".join(results_text)
            messages.append({"role": "user", "content": combined_results})

    # Exhausted turns
    final_screen = capture_pane(pane_info) if prev_screen else ""
    return SubtaskResult(
        subtask_id=subtask.id, status=SubtaskStatus.FAILED,
        summary=f"Exhausted {subtask.max_turns} turns without completing",
        output_snippet=(final_screen or prev_screen or "")[-500:],
        turns_used=subtask.max_turns, prompt_tokens=total_pt, completion_tokens=total_ct,
    )
