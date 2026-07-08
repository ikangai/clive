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
from evals.harness.metrics import EvalReport, EvalResult, ToolEvalResult


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


def _tool_result(task_id, flags_correct):
    """A minimal ToolEvalResult carrying just the flag-correctness signal."""
    return ToolEvalResult(
        task_id=task_id, layer=2, tool="shell", passed=flags_correct,
        turns_used=1, min_turns=1, prompt_tokens=0, completion_tokens=0,
        elapsed_seconds=0.0, detail="", flags_correct=flags_correct,
    )


def _plain_result(task_id, passed, turns_used, error_recovered=False,
                  false_completion=False):
    """A minimal EvalResult carrying just the reliability signals."""
    return EvalResult(
        task_id=task_id, layer=1, tool="shell", passed=passed,
        turns_used=turns_used, min_turns=1, prompt_tokens=0, completion_tokens=0,
        elapsed_seconds=0.0, detail="", error_recovered=error_recovered,
        false_completion=false_completion,
    )


def test_print_summary_surfaces_reliability_rates(monkeypatch):
    """error_recovery_rate and false_completion_rate are computed on every run
    and emitted in to_dict(), but were dark in the console summary — the operator
    reading print_summary() never saw the two most mission-relevant signals.
    print_summary() must now echo both reliability lines (gh#40 observability)."""
    import output

    report = EvalReport([
        _plain_result("a", passed=True, turns_used=2),
        _plain_result("b", passed=False, turns_used=2, error_recovered=True),
        _plain_result("c", passed=False, turns_used=2, error_recovered=False),
        _plain_result("d", passed=True, turns_used=2, false_completion=True),
    ])
    # errored = {b, c} (not passed); recovered among them = {b} -> 0.5
    assert report.error_recovery_rate == pytest.approx(0.5)
    # completed = 4 (all turns>0); false_completion = {d} -> 0.25
    assert report.false_completion_rate == pytest.approx(0.25)

    lines = []
    monkeypatch.setattr(output, "progress", lambda msg: lines.append(msg))
    report.print_summary()

    out = "\n".join(lines)
    er = f"{report.error_recovery_rate:.0%}"   # "50%"
    fc = f"{report.false_completion_rate:.0%}"  # "25%"
    assert any("Error recovery" in ln and er in ln for ln in lines), out
    assert any("False completion" in ln and fc in ln for ln in lines), out


def test_to_dict_surfaces_flag_accuracy():
    """flag_accuracy is computed on every tool run but was previously dark
    in reports. to_dict()['tool_metrics'] must now expose it, equal to the
    report's flag_accuracy property (gh#40 observability)."""
    report = EvalReport([
        _tool_result("a", flags_correct=True),
        _tool_result("b", flags_correct=False),
    ])
    assert report.tool_results, "report must contain ToolEvalResults"
    tool_metrics = report.to_dict()["tool_metrics"]
    assert "flag_accuracy" in tool_metrics
    assert tool_metrics["flag_accuracy"] == round(report.flag_accuracy, 3)
    # 1 of 2 flags correct -> 0.5, which is round-stable so equals the raw property.
    assert tool_metrics["flag_accuracy"] == report.flag_accuracy == pytest.approx(0.5)
