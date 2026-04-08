"""Executable skill runner — mechanically execute structured skill steps.

An executable skill has STEPS with commands, checks, and failure handlers.
The runner executes each step without LLM calls (happy path). Only calls
the LLM when a check fails and the on_fail action is "llm_repair".

This is the bridge between prose skills (injected as text, LLM follows)
and script mode (single bash script, no structure). Executable skills
have the structure of a plan, the efficiency of scripts, and the
recoverability of interactive mode.

Step format (in YAML within the skill file):
    STEPS:
    - cmd: curl -sI {URL} | head -1
      check: exit_code 0
      on_fail: skip
    - cmd: curl -s {URL} > {OUTPUT}
      check: file_exists {OUTPUT}
      on_fail: abort
"""
import json
import os
import re
import time

from models import PaneInfo, SubtaskResult, SubtaskStatus
from completion import wrap_command, wait_for_ready
from session import capture_pane
from output import progress


def parse_executable_steps(skill_content: str) -> list[dict]:
    """Parse STEPS section from a skill into structured step dicts.

    Returns list of: {cmd, check, check_type, check_value, on_fail, save}
    Returns empty list if no STEPS section found (prose-only skill).
    """
    # Find STEPS: section (steps start with "- cmd:" at any indentation)
    steps_match = re.search(r'STEPS:\s*\n((?:[-\s].+\n?)+)', skill_content)
    if not steps_match:
        return []

    steps = []
    current = None
    for line in steps_match.group(1).splitlines():
        line = line.strip()
        if line.startswith("- cmd:"):
            if current:
                steps.append(current)
            current = {
                "cmd": line[6:].strip(),
                "check": None,
                "check_type": None,
                "check_value": None,
                "on_fail": "abort",
                "save": None,
            }
        elif current and line.startswith("check:"):
            check = line[6:].strip()
            if check.startswith("exit_code"):
                current["check_type"] = "exit_code"
                current["check_value"] = check.split()[-1]
            elif check.startswith("file_exists"):
                current["check_type"] = "file_exists"
                current["check_value"] = check.split()[-1]
            elif check.startswith("output_contains"):
                current["check_type"] = "output_contains"
                current["check_value"] = check.split(None, 1)[-1]
            elif check.startswith("valid_json"):
                current["check_type"] = "valid_json"
            current["check"] = check
        elif current and line.startswith("on_fail:"):
            current["on_fail"] = line[8:].strip()
        elif current and line.startswith("save:"):
            current["save"] = line[5:].strip()

    if current:
        steps.append(current)
    return steps


def run_executable_skill(
    steps: list[dict],
    pane_info: PaneInfo,
    session_dir: str,
    params: dict | None = None,
    subtask_id: str = "skill",
) -> SubtaskResult:
    """Execute structured skill steps mechanically. No LLM on happy path.

    Returns SubtaskResult with step-by-step tracking.
    """
    params = params or {}
    total_steps = len(steps)
    completed_steps = 0
    outputs = []
    start_time = time.time()

    for i, step in enumerate(steps):
        cmd = step["cmd"]
        # Inject parameters
        for key, value in params.items():
            cmd = cmd.replace(f"{{{key.upper()}}}", value)
            cmd = cmd.replace(f"{{{key}}}", value)

        progress(f"    [skill step {i+1}/{total_steps}] {cmd[:60]}")

        # Execute command
        wrapped, marker = wrap_command(cmd, f"{subtask_id}_step{i}")
        pane_info.pane.send_keys(wrapped, enter=True)
        screen, method = wait_for_ready(pane_info, marker=marker, max_wait=30.0)

        # Parse exit code
        exit_code = None
        for line in screen.splitlines():
            if "EXIT:" in line and marker in line:
                try:
                    exit_code = int(line.split("EXIT:")[1].split()[0])
                except (ValueError, IndexError):
                    pass

        # Check verification
        check_passed = True
        if step["check_type"] == "exit_code":
            expected = int(step["check_value"])
            if exit_code is None:
                progress(f"    [skill step {i+1}] WARNING: exit code not captured")
                check_passed = False
            else:
                check_passed = (exit_code == expected)
        elif step["check_type"] == "file_exists":
            target = step["check_value"]
            for k, v in params.items():
                target = target.replace(f"{{{k.upper()}}}", v).replace(f"{{{k}}}", v)
            check_passed = os.path.exists(target)
        elif step["check_type"] == "output_contains":
            check_passed = step["check_value"] in screen
        elif step["check_type"] == "valid_json":
            try:
                content_lines = [l for l in screen.splitlines() if marker not in l and l.strip()]
                if not content_lines:
                    check_passed = False  # no output to validate
                else:
                    json.loads(content_lines[-1])
                    check_passed = True
            except (json.JSONDecodeError, IndexError):
                check_passed = False

        # Save output if requested
        if step.get("save") and check_passed:
            save_path = step["save"]
            for k, v in params.items():
                save_path = save_path.replace(f"{{{k.upper()}}}", v).replace(f"{{{k}}}", v)
            if not save_path.startswith("/"):
                save_path = os.path.join(session_dir, save_path)
            # Save the screen content (minus markers)
            clean = "\n".join(l for l in screen.splitlines() if marker not in l)
            with open(save_path, "w") as f:
                f.write(clean)

        outputs.append({
            "step": i + 1,
            "cmd": cmd[:80],
            "exit_code": exit_code,
            "check": step.get("check", "none"),
            "passed": check_passed,
        })

        if check_passed:
            completed_steps += 1
        else:
            progress(f"    [skill step {i+1}] CHECK FAILED: {step.get('check', '?')}")
            if step["on_fail"] == "skip":
                continue
            elif step["on_fail"] == "abort":
                break
            # Other on_fail actions (retry, llm_repair) could be added

    elapsed = time.time() - start_time
    all_passed = completed_steps == total_steps
    summary = f"Skill: {completed_steps}/{total_steps} steps completed"
    if all_passed:
        summary += f" in {elapsed:.1f}s"

    return SubtaskResult(
        subtask_id=subtask_id,
        status=SubtaskStatus.COMPLETED if all_passed else SubtaskStatus.FAILED,
        summary=summary,
        output_snippet=json.dumps(outputs, indent=2)[-500:],
        turns_used=0,  # no LLM calls on happy path
        prompt_tokens=0,
        completion_tokens=0,
        exit_code=0 if all_passed else 1,
    )
