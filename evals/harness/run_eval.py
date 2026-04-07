#!/usr/bin/env python3
"""Eval runner for clive.

Usage:
    python3 evals/harness/run_eval.py --layer 2 --tool shell
    python3 evals/harness/run_eval.py --layer 2
    python3 evals/harness/run_eval.py --all
    python3 evals/harness/run_eval.py --layer 2 --tool shell --driver drivers/shell_v2.md
"""
import argparse
import json
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evals.harness.session_fixture import EvalFixture
from evals.harness.verifier import verify_task
from evals.harness.metrics import EvalResult, EvalReport
from executor import run_subtask
from models import Subtask, PaneInfo
from llm import get_client


def _annotate_tasks(tasks: list[dict], source_dir: str) -> list[dict]:
    """Tag each task with the directory its tasks.json lives in."""
    for t in tasks:
        t["_source_dir"] = source_dir
    return tasks


def load_tasks(layer: int, tool: str | None = None) -> list[dict]:
    """Load task definitions for a layer (and optionally a specific tool)."""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if tool:
        tool_dir = os.path.join(base, f"layer{layer}", tool)
        tasks_path = os.path.join(tool_dir, "tasks.json")
        if os.path.exists(tasks_path):
            with open(tasks_path) as f:
                return _annotate_tasks(json.load(f), tool_dir)
        return []

    # Load all tools for this layer
    layer_dir = os.path.join(base, f"layer{layer}")
    if not os.path.isdir(layer_dir):
        return []

    all_tasks = []
    for tool_name in sorted(os.listdir(layer_dir)):
        tool_dir = os.path.join(layer_dir, tool_name)
        tasks_path = os.path.join(tool_dir, "tasks.json")
        if os.path.exists(tasks_path):
            with open(tasks_path) as f:
                all_tasks.extend(_annotate_tasks(json.load(f), tool_dir))
    return all_tasks


def run_planning_eval(task_def: dict) -> EvalResult:
    """Run a planning-only eval. Tests DAG structure, not execution."""
    task_id = task_def["id"]
    layer = task_def.get("layer", 4)
    start_time = time.time()

    # Create minimal pane setup for planning (no actual tmux needed)
    # The planner just needs tool info, not real panes
    tools_summary = task_def.get("tools_summary", """Available tools:
  - shell [shell]: Bash shell for commands
  - browser [browser]: Web browsing with lynx/curl""")

    try:
        from llm import get_client, chat
        from prompts import build_planner_prompt

        client = get_client()
        system_prompt = build_planner_prompt(tools_summary)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Task: {task_def['task']}"},
        ]
        content, pt, ct = chat(client, messages, max_tokens=2048)

        # Parse the plan JSON
        import re
        m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', content)
        if m:
            plan_json = json.loads(m.group(1))
        else:
            m = re.search(r'(\{[\s\S]*\})', content)
            plan_json = json.loads(m.group(1)) if m else {}

        subtasks = plan_json.get("subtasks", [])
        elapsed = time.time() - start_time

        # Check expectations
        expected = task_def.get("expected", {})
        checks_passed = True
        details = []

        # Check subtask count range
        if "min_subtasks" in expected:
            if len(subtasks) < expected["min_subtasks"]:
                checks_passed = False
                details.append(f"Too few subtasks: {len(subtasks)} < {expected['min_subtasks']}")
        if "max_subtasks" in expected:
            if len(subtasks) > expected["max_subtasks"]:
                checks_passed = False
                details.append(f"Too many subtasks: {len(subtasks)} > {expected['max_subtasks']}")

        # Check mode assignments
        if "expected_modes" in expected:
            for mode_check in expected["expected_modes"]:
                idx = mode_check.get("subtask_index", 0)
                exp_mode = mode_check["mode"]
                if idx < len(subtasks):
                    actual_mode = subtasks[idx].get("mode", "interactive")
                    if actual_mode != exp_mode:
                        checks_passed = False
                        details.append(f"Subtask {idx} mode: expected {exp_mode}, got {actual_mode}")

        # Check has_parallel (multiple subtasks with no deps)
        if "has_parallel" in expected:
            no_deps = [s for s in subtasks if not s.get("depends_on", [])]
            if expected["has_parallel"] and len(no_deps) < 2:
                checks_passed = False
                details.append("Expected parallel subtasks but none found")

        # Check has_dependencies
        if "has_dependencies" in expected:
            has_deps = any(s.get("depends_on", []) for s in subtasks)
            if expected["has_dependencies"] and not has_deps:
                checks_passed = False
                details.append("Expected dependencies but none found")

        detail = "; ".join(details) if details else "planning checks passed"

        return EvalResult(
            task_id=task_id, layer=layer, tool="planning",
            passed=checks_passed,
            turns_used=1, min_turns=1,
            prompt_tokens=pt, completion_tokens=ct,
            elapsed_seconds=elapsed,
            detail=detail,
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return EvalResult(
            task_id=task_id, layer=layer, tool="planning",
            passed=False, turns_used=0, min_turns=1,
            prompt_tokens=0, completion_tokens=0,
            elapsed_seconds=elapsed,
            detail=f"Exception: {e}",
        )


def run_single_task(
    task_def: dict,
    driver_override: str | None = None,
) -> EvalResult:
    """Run a single eval task and return the result."""
    if task_def.get("layer") == 4:
        return run_planning_eval(task_def)

    task_id = task_def["id"]
    tool = task_def.get("tool", "shell")
    layer = task_def.get("layer", 2)
    max_turns = task_def.get("max_turns", 8)

    # Resolve fixture directory (relative to the tasks.json source dir)
    source_dir = task_def.get("_source_dir", "")
    fixture_dir = None
    if "initial_state" in task_def and "filesystem" in task_def["initial_state"]:
        fixture_dir = os.path.join(source_dir, task_def["initial_state"]["filesystem"])

    # Clean shared state to prevent cross-test contamination
    import shutil
    if os.path.exists("/tmp/clive"):
        for f in os.listdir("/tmp/clive"):
            fp = os.path.join("/tmp/clive", f)
            if os.path.isfile(fp):
                os.unlink(fp)

    start_time = time.time()

    with EvalFixture(fixture_dir=fixture_dir, pane_app_type=tool) as ef:
        # Optionally override driver prompt
        if driver_override:
            os.environ["CLIVE_EVAL_DRIVER_OVERRIDE"] = driver_override

        # Register pane lock for executor compatibility
        from executor import _pane_locks
        import threading
        _pane_locks["eval"] = threading.Lock()

        # Ensure /tmp/clive exists (tasks reference it as absolute path)
        ef.send_keys("mkdir -p /tmp/clive", enter=True)
        time.sleep(0.3)

        # Create subtask for the worker
        subtask = Subtask(
            id=task_id,
            description=task_def["task"],
            pane="eval",
            max_turns=max_turns,
            mode=task_def.get("mode", "interactive"),
        )

        try:
            result = run_subtask(
                subtask=subtask,
                pane_info=ef.pane_info,
                dep_context="",
                session_dir="/tmp/clive",
            )

            elapsed = time.time() - start_time
            screen = ef.capture()

            # Verify
            passed, detail = verify_task(
                task_def,
                workdir=ef.workdir,
                agent_output=screen,
            )

            return EvalResult(
                task_id=task_id,
                layer=layer,
                tool=tool,
                passed=passed,
                turns_used=result.turns_used,
                min_turns=task_def.get("min_turns", 1),
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                elapsed_seconds=elapsed,
                detail=detail,
                false_completion=(
                    result.status.value == "completed" and not passed
                ),
            )
        except Exception as e:
            elapsed = time.time() - start_time
            return EvalResult(
                task_id=task_id,
                layer=layer,
                tool=tool,
                passed=False,
                turns_used=0,
                min_turns=task_def.get("min_turns", 1),
                prompt_tokens=0,
                completion_tokens=0,
                elapsed_seconds=elapsed,
                detail=f"Exception: {e}",
            )
        finally:
            if "CLIVE_EVAL_DRIVER_OVERRIDE" in os.environ:
                del os.environ["CLIVE_EVAL_DRIVER_OVERRIDE"]


def main():
    parser = argparse.ArgumentParser(description="clive eval runner")
    parser.add_argument("--layer", type=int, help="Layer to eval (2, 3, 4, 1)")
    parser.add_argument("--tool", help="Specific tool (e.g., shell, lynx)")
    parser.add_argument("--all", action="store_true", help="Run all evals")
    parser.add_argument("--driver", action="append", help="Driver prompt override(s)")
    parser.add_argument("--output", help="Save JSON report to file")
    parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 on any failure")
    parser.add_argument("--baseline", help="Baseline JSON for regression comparison")
    args = parser.parse_args()

    if not args.layer and not args.all:
        parser.error("Specify --layer N or --all")

    if args.all:
        tasks = []
        for layer in [2, 3, 4, 1]:
            tasks.extend(load_tasks(layer))
    else:
        tasks = load_tasks(args.layer, args.tool)

    if not tasks:
        print("No tasks found.", file=sys.stderr)
        sys.exit(1)

    print(f"Running {len(tasks)} eval tasks...", file=sys.stderr)

    results = []
    for task_def in tasks:
        driver = args.driver[0] if args.driver else None
        print(f"  [{task_def['id']}] {task_def['task'][:60]}...", file=sys.stderr)
        result = run_single_task(task_def, driver_override=driver)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.detail}", file=sys.stderr)

    report = EvalReport(results)
    report.print_summary()

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report.to_dict(), f, indent=2)
        print(f"Report saved to {args.output}", file=sys.stderr)

    # Baseline comparison
    if args.baseline:
        try:
            with open(args.baseline) as bf:
                baseline = json.load(bf)
            baseline_rate = baseline.get("completion_rate", 0)
            current_rate = report.completion_rate
            print(f"\nBaseline comparison:", file=sys.stderr)
            print(f"  Baseline: {baseline_rate:.0%}", file=sys.stderr)
            print(f"  Current:  {current_rate:.0%}", file=sys.stderr)
            if current_rate < baseline_rate:
                print(f"  REGRESSION detected", file=sys.stderr)
                if args.ci:
                    sys.exit(1)
            else:
                print(f"  OK: no regression", file=sys.stderr)
        except FileNotFoundError:
            print(f"  Baseline not found: {args.baseline}", file=sys.stderr)

    if args.ci and report.completion_rate < 1.0:
        sys.exit(1)


if __name__ == "__main__":
    main()
