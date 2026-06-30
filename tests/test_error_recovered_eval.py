"""Activate the dead `error_recovered` / `error_recovery_rate` eval metric.

``EvalReport.error_recovery_rate`` (metrics.py) has always aggregated the
``error_recovered`` field, but until now *nothing* populated it: every result
was constructed with the dataclass default ``error_recovered=False``, so the
rate reported a constant and could silently break.

The harness now computes the flag INLINE at result construction in
``run_eval.py`` via the pure helper ``_error_recovered``, mirroring the sibling
``false_completion`` one-liner that lives at both result constructors:

    error_recovered = (passed AND turns_used > min_turns)

i.e. a task that PASSED but needed MORE than its minimum number of turns had to
course-correct -- it recovered. A clean optimal pass (``turns_used ==
min_turns``) did not recover, and a failed run did not recover.

These tests pin the predicate directly (no live tmux run needed) and prove the
aggregate ``error_recovery_rate`` reads a mixed corpus correctly.
"""
from evals.harness.run_eval import _error_recovered
from evals.harness.metrics import EvalResult, EvalReport


def test_passed_run_with_extra_turns_recovered():
    """Passed but needed more than the minimum turns -> recovered."""
    assert _error_recovered(passed=True, turns_used=4, min_turns=2) is True


def test_clean_optimal_pass_did_not_recover():
    """Passed in exactly the minimum turns -> no course-correction needed."""
    assert _error_recovered(passed=True, turns_used=2, min_turns=2) is False


def test_failed_run_did_not_recover():
    """A failure is not a recovery, even with extra turns burned."""
    assert _error_recovered(passed=False, turns_used=9, min_turns=2) is False


def _mk(task_id, passed, turns_used, min_turns, error_recovered):
    return EvalResult(
        task_id, 2, "shell", passed=passed, turns_used=turns_used,
        min_turns=min_turns, prompt_tokens=10, completion_tokens=5,
        elapsed_seconds=1.0, detail="x", error_recovered=error_recovered,
    )


def test_report_error_recovery_rate_aggregates_mixed_list():
    """The aggregate metric surface: error_recovery_rate over a mixed list.

    error_recovery_rate = recovered / (recovered + failed); a clean optimal
    pass is neither (it never errored), so it is excluded from the denominator.
    """
    recovered = _mk("recovered", True, 4, 2, _error_recovered(True, 4, 2))
    clean = _mk("clean", True, 2, 2, _error_recovered(True, 2, 2))
    failed = _mk("failed", False, 9, 2, _error_recovered(False, 9, 2))

    # Sanity: the flags were populated by the predicate, not hand-set.
    assert recovered.error_recovered is True
    assert clean.error_recovered is False
    assert failed.error_recovered is False

    report = EvalReport([recovered, clean, failed])
    # Denominator = {recovered, failed} (clean never errored); 1 of 2 recovered.
    assert report.error_recovery_rate == 0.5
