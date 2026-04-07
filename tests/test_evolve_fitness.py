"""Tests for evolution fitness scoring."""
from evolve_fitness import fitness_score
from evals.harness.metrics import EvalResult, EvalReport


def _make_result(passed=True, turns=3, min_turns=2, tokens=3000):
    return EvalResult(
        task_id="test", layer=2, tool="shell", passed=passed,
        turns_used=turns, min_turns=min_turns,
        prompt_tokens=tokens // 2, completion_tokens=tokens // 2,
        elapsed_seconds=10.0, detail="test",
    )


def test_perfect_score():
    results = [_make_result(passed=True, turns=2, min_turns=2, tokens=1000)]
    report = EvalReport(results)
    score = fitness_score(report)
    assert score > 0.9


def test_failed_task_lowers_score():
    results = [
        _make_result(passed=True, turns=2, min_turns=2, tokens=1000),
        _make_result(passed=False, turns=5, min_turns=2, tokens=5000),
    ]
    report = EvalReport(results)
    score = fitness_score(report)
    assert score < 0.7


def test_more_turns_lowers_score():
    efficient = [_make_result(turns=2, min_turns=2, tokens=2000)]
    inefficient = [_make_result(turns=8, min_turns=2, tokens=2000)]
    score_eff = fitness_score(EvalReport(efficient))
    score_ineff = fitness_score(EvalReport(inefficient))
    assert score_eff > score_ineff


def test_more_tokens_lowers_score():
    cheap = [_make_result(turns=3, min_turns=2, tokens=1000)]
    expensive = [_make_result(turns=3, min_turns=2, tokens=20000)]
    score_cheap = fitness_score(EvalReport(cheap))
    score_expensive = fitness_score(EvalReport(expensive))
    assert score_cheap > score_expensive


def test_zero_tasks():
    report = EvalReport([])
    score = fitness_score(report)
    assert score == 0.0
