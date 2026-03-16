"""Tests for eval metrics collection."""
from evals.harness.metrics import EvalResult, EvalReport


def test_eval_result_creation():
    r = EvalResult(
        task_id="shell_001",
        layer=2,
        tool="shell",
        passed=True,
        turns_used=3,
        min_turns=2,
        prompt_tokens=500,
        completion_tokens=200,
        elapsed_seconds=4.5,
        detail="deterministic check passed",
    )
    assert r.passed is True
    assert r.turn_efficiency == 2 / 3


def test_eval_report_summary():
    results = [
        EvalResult("s1", 2, "shell", True, 3, 2, 500, 200, 4.5, "ok"),
        EvalResult("s2", 2, "shell", False, 8, 3, 800, 400, 12.0, "fail"),
        EvalResult("s3", 2, "shell", True, 2, 2, 300, 100, 2.0, "ok"),
    ]
    report = EvalReport(results)
    assert report.completion_rate == 2 / 3
    assert report.total_tasks == 3
    assert report.total_tokens == (500 + 200 + 800 + 400 + 300 + 100)
