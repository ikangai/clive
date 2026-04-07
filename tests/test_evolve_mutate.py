"""Tests for driver prompt mutation."""
from evolve_mutate import build_mutation_prompt, STRATEGIES


def test_strategies_exist():
    assert len(STRATEGIES) >= 3
    for s in STRATEGIES:
        assert "name" in s
        assert "goal" in s


def test_build_mutation_prompt_contains_driver():
    prompt = build_mutation_prompt(
        current_driver="# Shell Driver\nCOMMAND EXECUTION: one per turn",
        eval_summary="5/5 passed, avg 4 turns, 5000 tokens/task",
        strategy=STRATEGIES[0],
    )
    assert "Shell Driver" in prompt
    assert "5/5 passed" in prompt
    assert STRATEGIES[0]["goal"] in prompt


def test_build_mutation_prompt_has_constraints():
    prompt = build_mutation_prompt(
        current_driver="# Test\nshort",
        eval_summary="3/5 passed",
        strategy=STRATEGIES[0],
    )
    assert "80 lines" in prompt or "compact" in prompt.lower()
