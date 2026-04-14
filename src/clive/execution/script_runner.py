"""Script-mode subtask execution — generate → execute → verify → repair.

Extracted from executor.py. Imports shared primitives from runtime.py
(the leaf module), breaking the former circular dependency on executor.
"""

import json
import logging
import os
import threading
import uuid

from completion import wait_for_ready
from llm import get_client, chat, SCRIPT_MODEL
from models import Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from prompts import build_script_prompt
from runtime import _pane_locks, _cancel_event, _emit, _wrap_for_sandbox, write_file, _extract_script
from session import capture_pane

log = logging.getLogger(__name__)


def _resolve_script_language(script: str, session_dir: str, subtask_id: str, default_path: str) -> tuple[str, str]:
    """Detect script language from shebang. Returns (script_path, executor)."""
    if script.startswith("#!/usr/bin/env python") or script.startswith("#!/usr/bin/python"):
        return os.path.join(session_dir, f"_script_{subtask_id}.py"), "python3"
    return default_path, "bash"


def _audit_script_generation(subtask_id: str, attempt: int, script_path: str) -> None:
    """Best-effort audit logging — failures don't block execution."""
    try:
        from selfmod.audit import log_attempt
        log_attempt(
            proposal_id=f"script_{subtask_id}_{attempt}",
            action="script_generate",
            files=[script_path],
            tier="OPEN",
            roles={},
            gate_result={"allowed": True, "reason": "script generation"},
            outcome="generated",
            details=f"Script for subtask {subtask_id}, attempt {attempt}",
        )
    except Exception:
        pass


def _execute_script_in_pane(pane_info: PaneInfo, script_executor: str, script_path: str,
                            session_dir: str, subtask_id: str) -> tuple[str, int | None, str]:
    """Run a script in the pane and parse its exit code. Returns (screen, exit_code, nonce)."""
    nonce = uuid.uuid4().hex[:4]
    marker = f"___DONE_{subtask_id}_{nonce}___"
    script_cmd = _wrap_for_sandbox(
        f'{script_executor} {script_path}', session_dir, sandboxed=pane_info.sandboxed,
    )
    combined = f'{script_cmd}; echo "EXIT:$? {marker}"'
    pane_info.pane.send_keys(combined, enter=True)
    screen, _method = wait_for_ready(pane_info, marker=marker, max_wait=60.0)
    logging.debug(f"[{subtask_id}] Script attempt: {screen[-80:]}")

    exit_code: int | None = None
    for line in screen.splitlines():
        if marker in line and "EXIT:" in line:
            try:
                exit_code = int(line.split("EXIT:")[1].split()[0])
            except (ValueError, IndexError):
                pass
    return screen, exit_code, nonce


def _extract_script_output(screen: str, nonce: str, script_path: str, session_dir: str) -> str:
    """Strip markers, prompts and command echoes from the captured screen output."""
    nonce_frag = nonce + "___"
    output_lines = [
        l for l in screen.splitlines()
        if l.strip()
        and nonce_frag not in l
        and "___DONE_" not in l
        and "AGENT_READY" not in l
        and "export PS1=" not in l
        and not l.strip().startswith("EXIT:")
    ]
    cmd_echo = os.path.basename(script_path)
    while output_lines and (
        output_lines[0].strip().startswith(("$ bash ", "$ sh ", "$ python "))
        or output_lines[0].strip().startswith("$ /")
        or cmd_echo in output_lines[0]
        or output_lines[0].strip() == session_dir
        or output_lines[0].strip() == os.path.basename(session_dir)
    ):
        output_lines.pop(0)
    return "\n".join(output_lines)[-2000:] if output_lines else "Done (no output)"


def _write_script_success_artifacts(session_dir: str, subtask_id: str, summary: str,
                                    attempt: int, screen: str) -> None:
    """Persist the success result.json and execution log."""
    result_path = os.path.join(session_dir, f"_result_{subtask_id}.json")
    write_file(result_path, json.dumps({
        "status": "success",
        "subtask_id": subtask_id,
        "summary": summary,
        "turns_used": attempt,
    }, indent=2))
    log_path = os.path.join(session_dir, f"_log_{subtask_id}.txt")
    write_file(log_path, screen)


def run_subtask_script(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute a subtask in script mode: generate → execute → verify → repair loop."""
    log.info(f"Subtask {subtask.id}: script mode, pane={subtask.pane}")
    client = get_client()
    total_pt = 0
    total_ct = 0

    system_prompt = build_script_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Generate the script. Goal: {subtask.description}"},
    ]
    default_script_path = os.path.join(session_dir, f"_script_{subtask.id}.sh")

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
    with lock:
        for attempt in range(1, subtask.max_turns + 1):
            if _cancel_event.is_set():
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary="Cancelled", output_snippet="",
                    turns_used=attempt - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )
            effective_model = pane_info.agent_model or SCRIPT_MODEL
            reply, pt, ct = chat(client, messages, model=effective_model)
            total_pt += pt
            total_ct += ct
            messages.append({"role": "assistant", "content": reply})
            _emit(on_event, "turn", subtask.id, attempt, f"script gen attempt {attempt}")
            _emit(on_event, "tokens", subtask.id, pt, ct)

            try:
                script = _extract_script(reply)
            except ValueError as e:
                logging.debug(f"[{subtask.id}] Script extraction failed: {e}")
                messages.append({"role": "user", "content": "Error: could not extract script. Respond with a bash script inside ```bash ``` markers."})
                continue

            script_path, script_executor = _resolve_script_language(
                script, session_dir, subtask.id, default_script_path,
            )
            write_file(script_path, script)
            os.chmod(script_path, 0o755)
            _audit_script_generation(subtask.id, attempt, script_path)

            screen, exit_code, nonce = _execute_script_in_pane(
                pane_info, script_executor, script_path, session_dir, subtask.id,
            )

            if exit_code == 0:
                summary = _extract_script_output(screen, nonce, script_path, session_dir)
                _write_script_success_artifacts(session_dir, subtask.id, summary, attempt, screen)
                return SubtaskResult(
                    subtask_id=subtask.id,
                    status=SubtaskStatus.COMPLETED,
                    summary=summary,
                    output_snippet=screen[-500:] if len(screen) > 500 else screen,
                    turns_used=attempt,
                    prompt_tokens=total_pt,
                    completion_tokens=total_ct,
                )

            # Script failed — repair
            logging.debug(f"[{subtask.id}] Script failed (exit {exit_code}), repairing...")
            messages.append({
                "role": "user",
                "content": f"Script failed with exit code {exit_code}. Terminal output:\n\n{screen[-1000:]}\n\nFix the script and provide the corrected version.",
            })

    final_screen = capture_pane(pane_info)
    log_path = os.path.join(session_dir, f"_log_{subtask.id}.txt")
    write_file(log_path, final_screen)

    return SubtaskResult(
        subtask_id=subtask.id,
        status=SubtaskStatus.FAILED,
        summary=f"Script mode exhausted {subtask.max_turns} attempts",
        output_snippet=final_screen[-500:],
        turns_used=subtask.max_turns,
        prompt_tokens=total_pt,
        completion_tokens=total_ct,
    )
