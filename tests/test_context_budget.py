"""Tests for model-aware context budgeting."""


def test_cheap_model_gets_more_turns():
    from runtime import context_budget
    budget = context_budget("gemini-2.0-flash")
    assert budget["max_user_turns"] >= 6


def test_expensive_model_gets_fewer_turns():
    from runtime import context_budget
    budget = context_budget("claude-opus-4-20250514")
    assert budget["max_user_turns"] <= 3


def test_standard_model_gets_default():
    from runtime import context_budget
    budget = context_budget("claude-sonnet-4-20250514")
    assert budget["max_user_turns"] == 4


def test_unknown_model_gets_default():
    from runtime import context_budget
    budget = context_budget("some-unknown-model-v99")
    assert budget["max_user_turns"] == 4


def test_local_model_gets_more_turns():
    from runtime import context_budget
    budget = context_budget("llama3")
    assert budget["max_user_turns"] >= 6


def test_delegate_model_gets_default():
    from runtime import context_budget
    budget = context_budget("delegate")
    assert budget["max_user_turns"] == 4
