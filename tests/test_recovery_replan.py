"""Tests for summarizer.attempt_recovery's replan-and-retry trigger.

The recovery replan must fire not only when subtasks failed AND others were
skipped, but also for the most common autonomous case: a FAILED subtask with no
skipped dependents (a leaf failure or a single-subtask plan). Without this, the
first approach exhausting its turns / hitting an LLM error ends the run with no
alternative attempt — the 'premature termination' failure mode highlighted by
Terminal-Bench (arXiv 2601.11868). The len(failed)<=2 cap and the single-attempt
(non-recursive) bound are preserved.
"""
from unittest.mock import MagicMock, patch

from models import SubtaskResult, SubtaskStatus
from planning import summarizer


def _failed(sid, summary="failed"):
    return SubtaskResult(subtask_id=sid, status=SubtaskStatus.FAILED,
                         summary=summary, output_snippet="")


def _skipped(sid, summary="skipped"):
    return SubtaskResult(subtask_id=sid, status=SubtaskStatus.SKIPPED,
                         summary=summary, output_snippet="")


def _completed(sid, summary="ok"):
    return SubtaskResult(subtask_id=sid, status=SubtaskStatus.COMPLETED,
                         summary=summary, output_snippet="")


def _call(results, fake_execute, fake_create_plan):
    with patch.object(summarizer, "create_plan", fake_create_plan):
        return summarizer.attempt_recovery(
            task="do the thing",
            results=results,
            plan_execute_fn=fake_execute,
            panes={}, tool_status={}, tools_summary="",
            on_event=None, session_dir="", max_tokens=50000,
        )


def test_leaf_failure_with_no_skipped_triggers_replan():
    """A single FAILED subtask with no skipped dependents must still replan."""
    results = [_failed("1", "first approach exhausted turns")]
    recovery = [_completed("r1", "alternative approach worked")]
    fake_execute = MagicMock(return_value=recovery)
    replan = MagicMock()
    replan.subtasks = [MagicMock()]
    fake_create_plan = MagicMock(return_value=replan)

    out = _call(results, fake_execute, fake_create_plan)

    fake_create_plan.assert_called_once()
    fake_execute.assert_called_once()
    assert recovery[0] in out


def test_replan_prompt_includes_failure_when_no_skipped():
    """With no skipped subtasks, the reused replan prompt still carries the
    original task and the failure detail so the planner can try a fresh method."""
    results = [_failed("1", "DISTINCTIVE_FAILURE_SUMMARY")]
    fake_execute = MagicMock(return_value=[_completed("r1")])
    replan = MagicMock()
    replan.subtasks = [MagicMock()]
    captured = {}

    def fake_create_plan(task, *a, **k):
        captured["task"] = task
        return replan

    _call(results, fake_execute, fake_create_plan)

    assert "DISTINCTIVE_FAILURE_SUMMARY" in captured["task"]
    assert "do the thing" in captured["task"]


def test_failed_and_skipped_still_triggers_replan():
    """The original behaviour (failed + skipped) is preserved."""
    results = [_failed("1"), _skipped("2")]
    recovery = [_completed("r1")]
    fake_execute = MagicMock(return_value=recovery)
    replan = MagicMock()
    replan.subtasks = [MagicMock()]
    fake_create_plan = MagicMock(return_value=replan)

    out = _call(results, fake_execute, fake_create_plan)

    fake_create_plan.assert_called_once()
    assert recovery[0] in out


def test_three_failures_do_not_replan():
    """The len(failed)<=2 cap is preserved — larger failures don't replan."""
    results = [_failed("1"), _failed("2"), _failed("3")]
    fake_execute = MagicMock(return_value=[])
    fake_create_plan = MagicMock()

    out = _call(results, fake_execute, fake_create_plan)

    fake_create_plan.assert_not_called()
    assert out == results


def test_all_completed_does_not_replan():
    """No failures means nothing to recover — no replan."""
    results = [_completed("1")]
    fake_execute = MagicMock(return_value=[])
    fake_create_plan = MagicMock()

    out = _call(results, fake_execute, fake_create_plan)

    fake_create_plan.assert_not_called()
    assert out == results


def test_recovery_is_single_attempt_even_if_recovery_fails():
    """Single-attempt bound: recovery runs exactly once and does not recurse,
    even when the replanned approach also fails."""
    results = [_failed("1")]
    recovery = [_failed("r1", "recovery also failed")]
    fake_execute = MagicMock(return_value=recovery)
    replan = MagicMock()
    replan.subtasks = [MagicMock()]
    fake_create_plan = MagicMock(return_value=replan)

    out = _call(results, fake_execute, fake_create_plan)

    fake_create_plan.assert_called_once()
    fake_execute.assert_called_once()
    assert recovery[0] in out
