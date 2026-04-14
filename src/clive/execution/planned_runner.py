"""Planned-mode subtask execution — plan once, execute mechanically.

The LLM generates a full plan with verification criteria in ONE call.
The harness then executes each step mechanically — zero additional LLM
calls on happy path. This sits between script (monolithic) and
interactive (multi-turn): each step is verified independently.
"""

import json
import logging
import re
import threading

from dataclasses import dataclass, field

from completion import wrap_command, wait_for_ready
from llm import get_client, chat, SCRIPT_MODEL
from models import Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from prompts import build_planned_prompt
from runtime import (
    _pane_locks,
    _cancel_event,
    _emit,
    _check_command_safety,
    _wrap_for_sandbox,
)
from session import capture_pane

log = logging.getLogger(__name__)


# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class PlannedStep:
    cmd: str
    verify: str = "exit_code == 0"
    on_fail: str = "abort"


@dataclass
class PlannedPlan:
    steps: list[PlannedStep] = field(default_factory=list)
    done_summary: str = ""


# ─── Plan Parsing ─────────────────────────────────────────────────────────────

def parse_planned_steps(llm_response: str) -> PlannedPlan | None:
    """Parse a PlannedPlan from LLM response text.

    Handles both raw JSON and fenced ```json blocks.
    Returns None if parsing fails.
    """
    text = llm_response.strip()

    # Try extracting from fenced ```json block first
    m = re.search(r'```(?:json)?\s*\n([\s\S]*?)```', text)
    if m:
        text = m.group(1).strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict) or "steps" not in data:
        return None

    steps = []
    for s in data["steps"]:
        if not isinstance(s, dict) or "cmd" not in s:
            continue
        steps.append(PlannedStep(
            cmd=s["cmd"],
            verify=s.get("verify", "exit_code == 0"),
            on_fail=s.get("on_fail", "abort"),
        ))

    if not steps:
        return None

    return PlannedPlan(
        steps=steps,
        done_summary=data.get("done_summary", ""),
    )


# ─── Step Execution ──────────────────────────────────────────────────────────

def _execute_step(
    step: PlannedStep,
    pane_info: PaneInfo,
    subtask_id: str,
    step_index: int,
    session_dir: str,
) -> tuple[int | None, str]:
    """Execute a single planned step in the pane.

    Returns (exit_code, screen_content).
    """
    cmd = step.cmd
    cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)
    wrapped, marker = wrap_command(cmd, f"{subtask_id}_s{step_index}")
    pane_info.pane.send_keys(wrapped, enter=True)
    screen, _method = wait_for_ready(pane_info, marker=marker, max_wait=60.0)

    # Parse exit code from marker line
    exit_code: int | None = None
    for line in screen.splitlines():
        if marker in line and "EXIT:" in line:
            try:
                exit_code = int(line.split("EXIT:")[1].split()[0])
            except (ValueError, IndexError):
                pass

    return exit_code, screen


# ─── Main Runner ──────────────────────────────────────────────────────────────

def run_subtask_planned(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute a subtask in planned mode: 1 LLM call to plan, then mechanical execution."""
    log.info(f"Subtask {subtask.id}: planned mode, pane={subtask.pane}")

    # Phase 1: Generate the plan (single LLM call)
    client = get_client()
    system_prompt = build_planned_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Generate the plan. Goal: {subtask.description}"},
    ]

    effective_model = pane_info.agent_model or SCRIPT_MODEL
    reply, pt, ct = chat(client, messages, model=effective_model)
    _emit(on_event, "turn", subtask.id, 1, "plan generation")
    _emit(on_event, "tokens", subtask.id, pt, ct)

    # Phase 2: Parse the plan
    plan = parse_planned_steps(reply)
    if plan is None:
        return SubtaskResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="Failed to parse planned steps from LLM response",
            output_snippet=reply[:500],
            turns_used=1,
            prompt_tokens=pt,
            completion_tokens=ct,
        )

    # Phase 3: Execute steps mechanically
    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
    last_screen = ""
    steps_completed = 0

    with lock:
        for i, step in enumerate(plan.steps):
            if _cancel_event.is_set():
                return SubtaskResult(
                    subtask_id=subtask.id,
                    status=SubtaskStatus.FAILED,
                    summary=f"Cancelled after {steps_completed}/{len(plan.steps)} steps",
                    output_snippet=last_screen[-500:],
                    turns_used=1,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                )

            # Safety check
            violation = _check_command_safety(step.cmd)
            if violation:
                log.warning(f"[{subtask.id}] Step {i} blocked: {violation}")
                if step.on_fail == "abort":
                    return SubtaskResult(
                        subtask_id=subtask.id,
                        status=SubtaskStatus.FAILED,
                        summary=f"Step {i} blocked by safety check: {violation}",
                        output_snippet="",
                        turns_used=1,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                    )
                # skip or retry both just skip for safety violations
                continue

            exit_code, screen = _execute_step(
                step, pane_info, subtask.id, i, session_dir,
            )
            last_screen = screen

            if exit_code == 0:
                steps_completed += 1
                continue

            # Step failed — handle on_fail
            log.debug(f"[{subtask.id}] Step {i} failed (exit {exit_code}), on_fail={step.on_fail}")

            if step.on_fail == "retry":
                # Retry once
                exit_code2, screen2 = _execute_step(
                    step, pane_info, subtask.id, i, session_dir,
                )
                last_screen = screen2
                if exit_code2 == 0:
                    steps_completed += 1
                    continue
                # Retry also failed — abort
                return SubtaskResult(
                    subtask_id=subtask.id,
                    status=SubtaskStatus.FAILED,
                    summary=f"Step {i} failed after retry (exit {exit_code2}): {step.cmd}",
                    output_snippet=screen2[-500:],
                    turns_used=1,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                )

            if step.on_fail == "skip":
                steps_completed += 1  # count as progressed
                continue

            # Default: abort
            return SubtaskResult(
                subtask_id=subtask.id,
                status=SubtaskStatus.FAILED,
                summary=f"Step {i} failed (exit {exit_code}): {step.cmd}",
                output_snippet=screen[-500:],
                turns_used=1,
                prompt_tokens=pt,
                completion_tokens=ct,
            )

    # All steps completed
    summary = plan.done_summary or f"Completed {len(plan.steps)} planned steps"
    return SubtaskResult(
        subtask_id=subtask.id,
        status=SubtaskStatus.COMPLETED,
        summary=summary,
        output_snippet=last_screen[-500:],
        turns_used=1,
        prompt_tokens=pt,
        completion_tokens=ct,
    )
