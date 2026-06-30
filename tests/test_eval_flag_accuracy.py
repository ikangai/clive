"""Layer-2 flag-accuracy eval fixtures (gh#40 follow-up to the synthetic
unit tests in test_eval_discovery.py).

task-9840dcef wired the `expected_flags` discovery criterion + the
`flag_accuracy` aggregate, but exercised them only with hand-built
scrollback strings inside the test module. These fixtures ship *real*
Layer-2 eval scenarios (loaded by run_eval) whose discovery_criteria
carry `expected_flags`, so a live `--layer 2` run computes flag_accuracy
over actual scenarios. Here we prove that offline: each shipped fixture's
criterion is evaluated by check_discovery_criteria against synthetic
scrollback, and the resulting ToolEvalResults feed flag_accuracy.
"""
import json
import os

import pytest

from evals.harness.discovery_eval import PROMPT_MARKER, check_discovery_criteria
from evals.harness.metrics import EvalReport, ToolEvalResult


FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "evals", "layer2", "flag_accuracy",
)


def _load_tasks():
    with open(os.path.join(FIXTURE_DIR, "tasks.json")) as f:
        return json.load(f)


def _scrollback(cmd):
    return f"{PROMPT_MARKER} {cmd}\n"


# A correct command line per task: the expected tool invoked WITH its
# required flag (stands in for what a competent agent would type).
GOOD_CMDS = {
    "l2_flag_jq_raw_001": "jq -r '.[].email' users.json > /tmp/clive/result.txt",
    "l2_flag_rg_icase_002": "rg -i error app.log | wc -l > /tmp/clive/result.txt",
    "l2_flag_sort_numeric_003": "sort -n values.txt > /tmp/clive/result.txt",
}

# The same command with the required flag OMITTED — wrong/lossy behaviour.
BAD_CMDS = {
    "l2_flag_jq_raw_001": "jq '.[].email' users.json > /tmp/clive/result.txt",
    "l2_flag_rg_icase_002": "rg error app.log | wc -l > /tmp/clive/result.txt",
    "l2_flag_sort_numeric_003": "sort values.txt > /tmp/clive/result.txt",
}


def _result_from(task, scrollback):
    """Run a fixture's discovery_criteria and wrap it as a ToolEvalResult,
    mirroring run_eval.run_single_task's `**disc_fields` construction."""
    ok, fields, detail = check_discovery_criteria(
        task["discovery_criteria"], scrollback
    )
    result = ToolEvalResult(
        task_id=task["id"], layer=2, tool="shell", passed=ok,
        turns_used=1, min_turns=1, prompt_tokens=0, completion_tokens=0,
        elapsed_seconds=0.0, detail=detail, **fields,
    )
    return ok, fields, detail, result


def test_fixtures_carry_expected_flags():
    tasks = _load_tasks()
    assert 2 <= len(tasks) <= 3
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate eval ids"
    assert set(ids) == set(GOOD_CMDS), "test command map out of sync with fixtures"
    for t in tasks:
        assert t["layer"] == 2
        assert t["mode"] == "script"
        assert t["success_criteria"]["type"] == "deterministic"
        assert t["success_criteria"]["check"]
        # The whole point of this fixture set: a real expected_flags criterion.
        flags = t["discovery_criteria"].get("expected_flags")
        assert flags, f"{t['id']} must carry expected_flags"
        assert all(v for v in flags.values()), "empty flag list"
        assert t["max_turns"] >= t["min_turns"] >= 1


def test_fixture_filesystems_exist():
    for t in _load_tasks():
        fs = t.get("initial_state", {}).get("filesystem")
        assert fs, f"{t['id']} missing initial_state.filesystem"
        assert os.path.isdir(os.path.join(FIXTURE_DIR, fs)), f"missing fixture {fs}"


def test_each_fixture_yields_flags_correct_on_good_invocation():
    """Real scenario + correct flag usage -> flags_correct True and the
    criterion passes. This is the end-to-end exercise of flag_accuracy
    over real fixtures (not the synthetic unit tests)."""
    results = []
    for t in _load_tasks():
        ok, fields, detail, result = _result_from(t, _scrollback(GOOD_CMDS[t["id"]]))
        assert fields["flags_correct"] is True, f"{t['id']}: {detail}"
        assert ok, f"{t['id']}: {detail}"
        results.append(result)

    report = EvalReport(results)
    assert report.tool_results, "fixtures must produce ToolEvalResults"
    # flag_accuracy is now computed over REAL scenarios, all flags correct.
    assert report.flag_accuracy == pytest.approx(1.0)


def test_omitting_required_flag_lowers_flag_accuracy():
    """Omitting the required flag flips flags_correct to False and pulls
    flag_accuracy down — proving the metric reacts to real evidence, not a
    constant."""
    results = []
    for t in _load_tasks():
        ok, fields, detail, result = _result_from(t, _scrollback(BAD_CMDS[t["id"]]))
        assert fields["flags_correct"] is False, t["id"]
        assert not ok, f"{t['id']} should fail: {detail}"
        assert "required flag" in detail
        results.append(result)

    report = EvalReport(results)
    assert report.flag_accuracy == pytest.approx(0.0)


def test_expected_tool_still_checked_alongside_flags():
    """The fixtures keep an expected_tool criterion too; a flags-correct
    run also satisfies tool selection."""
    for t in _load_tasks():
        _, fields, _, _ = _result_from(t, _scrollback(GOOD_CMDS[t["id"]]))
        assert fields["tool_correct"] is True
        assert fields["tool_used"] is not None
