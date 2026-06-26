"""False-completion eval scenarios activate the dead `false_completion` metric (#40).

The harness computes ``false_completion = (status == "completed" AND not verifier.passed)``
in ``run_eval.py`` (the EvalResult branch at ~309-313 and the ToolEvalResult branch at
~319-321), and ``EvalReport.false_completion_rate`` (metrics.py) reports it. But until now
*no* eval task was designed to provoke it: every scenario's obvious command also happens to
be correct, so an agent that skips self-verification still passes. The metric therefore
never fired and could silently break.

These tests pin down a small Layer 2 corpus whose naive/obvious command yields a subtly
WRONG result (wrong JSON field, off-by-one count, wrong output path). For each scenario we
assert:

  * the naive ("trap") command FAILS the deterministic ``success_criteria`` -- so an agent
    that emits DONE without checking is recorded as a false completion;
  * the harness formula therefore records ``false_completion == True``;
  * the correct command PASSES and records no false completion -- proving the trap is real
    and escapable, not an impossible task.

This is the failing-eval half of the DONE-verification-gate TDD split for #40: the gate
(separate change) makes a self-verifying agent stop emitting DONE on these traps, driving
the live false_completion rate back down.
"""
import json
import os
import shutil
import subprocess

import pytest

from evals.harness.verifier import verify_task
from evals.harness.metrics import EvalResult, EvalReport

SCENARIOS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "evals", "layer2", "false_completion",
)
TASKS_PATH = os.path.join(SCENARIOS_DIR, "tasks.json")


def _load_scenarios():
    with open(TASKS_PATH) as f:
        return json.load(f)


def _setup_workdir(task, workdir):
    """Replicate the harness fixture copy (session_fixture.py): copy the
    contents of ``initial_state.filesystem`` into a clean workdir."""
    fs = task.get("initial_state", {}).get("filesystem")
    if not fs:
        return
    src = os.path.join(SCENARIOS_DIR, fs)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(workdir, item)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


def _run(cmd, workdir):
    subprocess.run(
        cmd, shell=True, cwd=workdir, check=True,
        capture_output=True, timeout=10,
    )


def _false_completion(status, passed):
    """The exact rule the harness applies (run_eval.py:309-313 / 319-321)."""
    return status == "completed" and not passed


# Loaded once at import so the suite turns RED loudly if the corpus is missing.
SCENARIOS = _load_scenarios()
_IDS = [t["id"] for t in SCENARIOS]


def test_corpus_is_deterministic_layer_2_or_3():
    """Each scenario must be a deterministic Layer 2/3 task carrying both the
    naive (trap) command and the correct command used to exercise the metric."""
    assert SCENARIOS, "false_completion corpus must define at least one scenario"
    assert len(SCENARIOS) >= 2, "task asks for 2-3 false-completion scenarios"
    for t in SCENARIOS:
        assert t["layer"] in (2, 3), f"{t['id']}: must be Layer 2/3"
        assert t["success_criteria"]["type"] == "deterministic", t["id"]
        assert t["naive_command"], f"{t['id']}: needs a naive (trap) command"
        assert t["correct_command"], f"{t['id']}: needs a correct command"


@pytest.mark.parametrize("task", SCENARIOS, ids=_IDS)
def test_naive_command_provokes_false_completion(task, tmp_path):
    """The obvious command produces a wrong result -> verifier fails -> a DONE
    self-report is recorded as false_completion."""
    work = str(tmp_path)
    _setup_workdir(task, work)
    _run(task["naive_command"], work)

    passed, detail = verify_task(task, workdir=work)
    assert passed is False, (
        f"{task['id']}: naive command unexpectedly satisfied success_criteria - "
        f"the trap is broken ({detail})"
    )
    # Agent emitted DONE (status == completed) without self-verifying.
    assert _false_completion("completed", passed) is True


@pytest.mark.parametrize("task", SCENARIOS, ids=_IDS)
def test_correct_command_has_no_false_completion(task, tmp_path):
    """The correct command satisfies success_criteria, so an honest DONE is not
    flagged -- proving each trap is escapable, not an impossible task."""
    work = str(tmp_path)
    _setup_workdir(task, work)
    _run(task["correct_command"], work)

    passed, detail = verify_task(task, workdir=work)
    assert passed is True, (
        f"{task['id']}: correct command failed success_criteria - "
        f"the task is unsatisfiable ({detail})"
    )
    assert _false_completion("completed", passed) is False


def test_report_false_completion_rate_counts_a_trap():
    """The aggregate metric surface for #40: a completed-but-unverified result
    lands in false_completion_rate (previously untested)."""
    honest = EvalResult(
        "ok", 2, "shell", passed=True, turns_used=3, min_turns=2,
        prompt_tokens=10, completion_tokens=5, elapsed_seconds=1.0,
        detail="ok", false_completion=False,
    )
    trap = EvalResult(
        "trap", 2, "shell", passed=False, turns_used=4, min_turns=2,
        prompt_tokens=10, completion_tokens=5, elapsed_seconds=1.0,
        detail="naive wrong output", false_completion=True,
    )
    report = EvalReport([honest, trap])
    assert report.false_completion_rate == 0.5
