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

# Add repo root (for `from evals...`) and src/clive (flat imports) to path
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.join(_project_root, "src", "clive"))

from evals.harness.session_fixture import EvalFixture
from evals.harness.verifier import verify_task
from evals.harness.metrics import EvalResult, EvalReport, ToolEvalResult
from evals.harness.discovery_eval import (
    build_discovery_context,
    check_discovery_criteria,
    make_disabled_tool_shims,
)
from executor import run_subtask
from models import Subtask, PaneInfo
from llm import get_client


def _error_recovered(passed: bool, turns_used: int, min_turns: int) -> bool:
    """Whether a task recovered from an error: it PASSED but needed MORE than
    its minimum number of turns, i.e. it had to course-correct.

    Pure predicate (no I/O) so it is unit-testable without a live tmux run;
    mirrors the inline ``false_completion`` rule at the result constructors.
    """
    return bool(passed and turns_used > min_turns)


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
    initial_state = task_def.get("initial_state", {})
    # Layer 5: agent starts at a registry tier and must discover tools.
    # Other layers may still carry discovery_criteria (e.g. L3 pipelines
    # verify which tools were chained) without the context injection.
    inject_discovery = layer == 5 or "registry_tier" in initial_state
    check_discovery = inject_discovery or "discovery_criteria" in task_def

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

    # Discovery tasks run in a plain shell pane; "discovery" is not a driver.
    pane_app = "shell" if check_discovery else tool

    prev_progressive = os.environ.get("CLIVE_PROGRESSIVE_TOOLS")

    with EvalFixture(fixture_dir=fixture_dir, pane_app_type=pane_app) as ef:
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

        dep_context = ""
        if inject_discovery:
            os.environ["CLIVE_PROGRESSIVE_TOOLS"] = "1"
            # Make clive-tools reachable in the pane; shim away disabled
            # tools so fallback evals behave the same on every host.
            path_parts = []
            shim_dir = make_disabled_tool_shims(
                os.path.join(ef.workdir, ".disabled_shims"),
                initial_state.get("disabled_tools", []),
            )
            if shim_dir:
                path_parts.append(shim_dir)
            path_parts.append(os.path.join(_project_root, "tools"))
            ef.send_keys(
                'export PATH="' + ":".join(path_parts) + ':$PATH"', enter=True
            )
            time.sleep(0.2)

        # Create subtask for the worker. Discovery context goes into the
        # description (the trusted GOAL slot — in an eval the harness is
        # the planner), NOT dep_context: build_interactive_prompt wraps
        # dep_context in UNTRUSTED/DO-NOT-FOLLOW markers (Audit H19/H20),
        # so instructions placed there are correctly ignored by the model.
        description = task_def["task"]
        if inject_discovery:
            description += "\n\n" + build_discovery_context(
                initial_state.get("registry_tier", 0)
            )
        subtask = Subtask(
            id=task_id,
            description=description,
            pane="eval",
            max_turns=max_turns,
            mode=task_def.get("mode", "interactive"),
        )

        try:
            result = run_subtask(
                subtask=subtask,
                pane_info=ef.pane_info,
                dep_context=dep_context,
                session_dir="/tmp/clive",
            )

            elapsed = time.time() - start_time
            screen = ef.capture()

            # Verify outcome
            passed, detail = verify_task(
                task_def,
                workdir=ef.workdir,
                agent_output=screen,
            )

            common = dict(
                task_id=task_id,
                layer=layer,
                tool=tool,
                turns_used=result.turns_used,
                min_turns=task_def.get("min_turns", 1),
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                elapsed_seconds=elapsed,
            )

            if check_discovery:
                # Verify process: discovery navigation + tool choice.
                # Script mode hides the pipeline inside the generated
                # script file, so include those as evidence.
                import glob
                script_text = ""
                for path in glob.glob(f"/tmp/clive/_script_{task_id}*"):
                    try:
                        with open(path) as sf:
                            script_text += sf.read() + "\n"
                    except OSError:
                        pass
                scrollback = ef.capture_scrollback()
                if os.environ.get("CLIVE_EVAL_DUMP_SCROLLBACK") == "1":
                    with open(f"/tmp/clive_eval_scrollback_{task_id}.txt", "w") as df:
                        df.write(scrollback)
                disc_ok, disc_fields, disc_detail = check_discovery_criteria(
                    task_def.get("discovery_criteria", {}),
                    scrollback,
                    script_text=script_text,
                )
                passed = passed and disc_ok
                return ToolEvalResult(
                    passed=passed,
                    detail=f"{detail}; {disc_detail}",
                    error_recovered=_error_recovered(
                        passed, result.turns_used, task_def.get("min_turns", 1)
                    ),
                    false_completion=(
                        result.status.value == "completed" and not passed
                    ),
                    **common,
                    **disc_fields,
                )

            return EvalResult(
                passed=passed,
                detail=detail,
                error_recovered=_error_recovered(
                    passed, result.turns_used, task_def.get("min_turns", 1)
                ),
                false_completion=(
                    result.status.value == "completed" and not passed
                ),
                **common,
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
            if inject_discovery:
                if prev_progressive is None:
                    os.environ.pop("CLIVE_PROGRESSIVE_TOOLS", None)
                else:
                    os.environ["CLIVE_PROGRESSIVE_TOOLS"] = prev_progressive


def run_comparison(args, tasks):
    """Run the same tasks with two different models and compare."""
    import os
    results = {}
    for model in args.compare:
        os.environ["AGENT_MODEL"] = model
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(f"Running with model: {model}", file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)

        model_results = []
        for task_def in tasks:
            if task_def.get("layer") == 4:
                result = run_planning_eval(task_def)
            else:
                result = run_single_task(task_def)
            model_results.append(result)
        results[model] = EvalReport(model_results)

    # Print comparison
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"MODEL COMPARISON", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    print(f"{'':30s} {'Model A':>15s} {'Model B':>15s}", file=sys.stderr)
    print(f"{'':30s} {args.compare[0]:>15s} {args.compare[1]:>15s}", file=sys.stderr)
    print(f"{'-' * 60}", file=sys.stderr)

    for name, (a, b) in [
        ("Pass rate", (results[args.compare[0]].completion_rate, results[args.compare[1]].completion_rate)),
        ("Turn efficiency", (results[args.compare[0]].avg_turn_efficiency, results[args.compare[1]].avg_turn_efficiency)),
        ("Total tokens", (results[args.compare[0]].total_tokens, results[args.compare[1]].total_tokens)),
        ("Total time (s)", (results[args.compare[0]].total_elapsed, results[args.compare[1]].total_elapsed)),
    ]:
        print(f"  {name:28s} {a:>15.3f} {b:>15.3f}", file=sys.stderr)

    cost_a = results[args.compare[0]].estimated_cost()
    cost_b = results[args.compare[1]].estimated_cost()
    if cost_a > 0 or cost_b > 0:
        print(f"  {'Est. cost ($)':28s} {cost_a:>15.4f} {cost_b:>15.4f}", file=sys.stderr)
    print(f"{'=' * 60}\n", file=sys.stderr)

    # Clean up
    if "AGENT_MODEL" in os.environ:
        del os.environ["AGENT_MODEL"]


def main():
    parser = argparse.ArgumentParser(description="clive eval runner")
    parser.add_argument("--layer", type=int, help="Layer to eval (2, 3, 4, 1, 5)")
    parser.add_argument("--tool", help="Specific tool (e.g., shell, lynx)")
    parser.add_argument("--task", help="Run only the task with this id")
    parser.add_argument(
        "--refine", metavar="TOOL",
        help="After the run, refine TOOL's driver from failure signals "
             "(gh#41 Phase 3 loop; writes to drivers/.unreviewed/)")
    parser.add_argument("--all", action="store_true", help="Run all evals")
    parser.add_argument("--driver", action="append", help="Driver prompt override(s)")
    parser.add_argument("--output", help="Save JSON report to file")
    parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 on any failure")
    parser.add_argument("--baseline", help="Baseline JSON for regression comparison")
    parser.add_argument("--compare", nargs=2, metavar="MODEL",
                        help="Compare two models (e.g., --compare gpt-4o claude-sonnet-4-20250514)")
    args = parser.parse_args()

    if not args.layer and not args.all:
        parser.error("Specify --layer N or --all")

    if args.all:
        tasks = []
        for layer in [2, 3, 4, 1, 5]:
            tasks.extend(load_tasks(layer))
    else:
        tasks = load_tasks(args.layer, args.tool)

    if args.task:
        tasks = [t for t in tasks if t["id"] == args.task]

    if not tasks:
        print("No tasks found.", file=sys.stderr)
        sys.exit(1)

    if args.compare:
        run_comparison(args, tasks)
        return

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

    if args.refine:
        from evals.harness.refine_loop import refine_from_results
        path = refine_from_results(args.refine, results)
        if path:
            print(f"Refined driver written to {path} — review and "
                  f"`clive --promote-driver {args.refine}` to activate.",
                  file=sys.stderr)
        else:
            print(f"No refinement for {args.refine} (no matching failure "
                  f"signals or no existing driver).", file=sys.stderr)

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
