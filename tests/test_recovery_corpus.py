"""Recovery-path eval corpus: exercises the now-active `error_recovered` metric.

task-5549e2cb activated ``EvalResult.error_recovered``: the harness now populates
it inline in ``run_eval.py`` via the pure helper ``_error_recovered`` and
``EvalReport.error_recovery_rate`` (metrics.py) aggregates it. The flag fires when
a task PASSED but needed MORE than its minimum number of turns -- i.e. it hit an
obstacle and had to course-correct:

    error_recovered = (passed AND turns_used > min_turns)

Until now no eval scenario was *designed* to provoke a recovery: every shipped
scenario's obvious command also happens to work, so the metric only ever read a
constant and could silently break. This corpus ships a Layer-2 recovery scenario
whose obvious first command FAILS (a visible, recoverable obstacle) while a viable
retry SUCCEEDS, with ``min_turns`` low enough that a recovering pass (one failed
try + one successful retry, ``turns_used = min_turns + 1``) yields
``turns_used > min_turns`` -> ``error_recovered = True``.

These tests are pure loader/fixture checks -- no LLM and no live tmux run. They
load the corpus through the SAME harness loader ``run_eval`` uses
(``load_tasks``, which auto-discovers ``evals/layer2/<dir>/tasks.json`` via
``os.listdir`` with no hardcoded registry), assert the recovery dir is
discovered, that each scenario mirrors the ``false_completion`` field shape
exactly, and that the trap is genuinely recovery-shaped (naive command fails the
deterministic check, correct command passes).
"""
import json
import os
import shutil
import subprocess

import pytest

from evals.harness.run_eval import load_tasks, _error_recovered
from evals.harness.verifier import verify_task


EVALS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "evals",
)
FALSE_COMPLETION_TASKS = os.path.join(
    EVALS_DIR, "layer2", "false_completion", "tasks.json"
)

# The exact per-scenario field shape the recovery corpus must mirror, taken from
# the sibling false_completion corpus (the brief: "mirror its shape EXACTLY").
# load_tasks() additionally tags each loaded task with a `_source_dir` key.
with open(FALSE_COMPLETION_TASKS) as _f:
    _FALSE_COMPLETION = json.load(_f)
REFERENCE_FIELDS = set(_FALSE_COMPLETION[0].keys())


def _recovery_scenarios():
    """Load the recovery corpus through the harness loader (explicit tool)."""
    return load_tasks(2, "recovery")


# Loaded once at import so the module turns RED loudly if the corpus disappears.
SCENARIOS = _recovery_scenarios()
_IDS = [t["id"] for t in SCENARIOS]


def test_recovery_dir_auto_discovered():
    """run_eval's loader walks layer2/ with os.listdir (no hardcoded registry),
    so the new recovery dir is picked up by the layer-wide load with zero edit to
    run_eval.py -- proving the >=1 scenario is actually discoverable, not just on
    disk."""
    all_layer2 = load_tasks(2)            # no tool -> os.listdir auto-discovery
    discovered = {t["id"] for t in all_layer2}
    assert SCENARIOS, "recovery corpus must define at least one scenario"
    assert 1 <= len(SCENARIOS) <= 2, "brief caps the corpus at 1-2 scenarios"
    for t in SCENARIOS:
        assert t["id"] in discovered, (
            f"{t['id']} not auto-discovered by load_tasks(2)"
        )


def test_scenarios_mirror_false_completion_shape():
    """Each recovery scenario carries exactly the false_completion field set
    (plus the loader's `_source_dir` tag): id/layer/tool/mode/task/initial_state/
    success_criteria/trap/naive_command/correct_command/min_turns/max_turns/
    timeout_seconds."""
    ids = [t["id"] for t in SCENARIOS]
    assert len(ids) == len(set(ids)), "duplicate eval ids"
    for t in SCENARIOS:
        fields = set(t.keys()) - {"_source_dir"}
        assert fields == REFERENCE_FIELDS, (
            f"{t['id']}: field shape diverges from false_completion -- "
            f"missing={REFERENCE_FIELDS - fields}, extra={fields - REFERENCE_FIELDS}"
        )
        assert t["layer"] == 2
        assert t["tool"]
        assert t["mode"] == "interactive"
        assert t["task"].strip()
        assert t["success_criteria"]["type"] == "deterministic"
        assert t["success_criteria"]["check"].strip()
        assert t["trap"].strip()
        assert t["naive_command"].strip()
        assert t["correct_command"].strip()
        assert t["timeout_seconds"] > 0


def test_min_turns_below_max_and_low_enough_for_recovery():
    """min_turns < max_turns, and min_turns is low enough that a recovering pass
    (one failed try + one successful retry, turns_used = min_turns + 1) trips
    error_recovered, while a clean optimal pass at the minimum does not."""
    for t in SCENARIOS:
        mn, mx = t["min_turns"], t["max_turns"]
        assert 1 <= mn < mx, f"{t['id']}: need 1 <= min_turns < max_turns"
        # A recovering pass burns at least one extra turn -> error_recovered.
        assert _error_recovered(passed=True, turns_used=mn + 1, min_turns=mn) is True
        # A clean optimal pass at the minimum did not recover.
        assert _error_recovered(passed=True, turns_used=mn, min_turns=mn) is False


def test_fixture_filesystems_exist():
    """Every scenario's initial_state.filesystem resolves to a real dir under the
    loader-tagged source directory."""
    for t in SCENARIOS:
        fs = t.get("initial_state", {}).get("filesystem")
        assert fs, f"{t['id']} missing initial_state.filesystem"
        src = os.path.join(t["_source_dir"], fs)
        assert os.path.isdir(src), f"{t['id']}: missing fixture dir {src}"


def _setup_workdir(task, workdir):
    """Replicate the harness fixture copy: copy the contents of
    initial_state.filesystem into a clean workdir (resolved against the loader's
    `_source_dir` tag)."""
    fs = task["initial_state"]["filesystem"]
    src = os.path.join(task["_source_dir"], fs)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(workdir, item)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


def _run(cmd, workdir, check):
    return subprocess.run(
        cmd, shell=True, cwd=workdir, check=check,
        capture_output=True, timeout=10,
    )


@pytest.mark.parametrize("task", SCENARIOS, ids=_IDS)
def test_trap_is_recovery_shaped(task, tmp_path):
    """The obvious first command FAILS the deterministic check (the recoverable
    obstacle); the retry PASSES it. So a recovering agent's run is a genuine
    course-correction (turns_used > min_turns -> error_recovered), not an
    impossible task."""
    # naive (trap) attempt: it may even exit non-zero -- that IS the visible,
    # recoverable obstacle. We assert on the verifier outcome, not the exit code.
    naive_dir = os.path.join(str(tmp_path), "naive")
    os.makedirs(naive_dir)
    _setup_workdir(task, naive_dir)
    _run(task["naive_command"], naive_dir, check=False)
    naive_passed, naive_detail = verify_task(task, workdir=naive_dir)
    assert naive_passed is False, (
        f"{task['id']}: naive command unexpectedly satisfied success_criteria -- "
        f"the recovery obstacle is broken ({naive_detail})"
    )

    # recovery: the viable retry satisfies the deterministic check cleanly.
    correct_dir = os.path.join(str(tmp_path), "correct")
    os.makedirs(correct_dir)
    _setup_workdir(task, correct_dir)
    _run(task["correct_command"], correct_dir, check=True)
    correct_passed, correct_detail = verify_task(task, workdir=correct_dir)
    assert correct_passed is True, (
        f"{task['id']}: correct command failed success_criteria -- the task is "
        f"unsatisfiable ({correct_detail})"
    )
