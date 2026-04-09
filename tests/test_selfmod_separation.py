"""Tests for information barriers between selfmod roles."""

import os
import json


def test_reviewer_does_not_see_proposer_reasoning():
    """The reviewer prompt must not contain proposer's description or rationale."""
    from selfmod.reviewer import build_review_prompt
    proposal = {
        "description": "SECRET_PROPOSER_REASONING",
        "rationale": "SECRET_PROPOSER_RATIONALE",
        "files": {"test.py": "print('hello')"},
    }
    prompt = build_review_prompt(proposal, {"test.py": "# old"})
    assert "SECRET_PROPOSER_REASONING" not in prompt
    assert "SECRET_PROPOSER_RATIONALE" not in prompt
    # But the diff/files should be present
    assert "test.py" in prompt


def test_auditor_does_not_see_reviewer_reasoning():
    """The auditor prompt must not contain reviewer's full reasoning."""
    from selfmod.auditor import build_audit_prompt
    proposal = {
        "description": "change stuff",
        "rationale": "because",
        "files": {"test.py": "print('hello')"},
    }
    review = {
        "verdict": "approved",
        "issues": [],
        "reasoning": "SECRET_REVIEWER_REASONING",
        "risk_assessment": "low",
    }
    prompt = build_audit_prompt(proposal, review)
    assert "SECRET_REVIEWER_REASONING" not in prompt
    # But verdict should be present
    assert "approved" in prompt


def test_proposer_temperature():
    """Proposer must use temperature 0.7."""
    from selfmod.proposer import PROPOSER_TEMPERATURE
    assert PROPOSER_TEMPERATURE == 0.7


def test_reviewer_temperature():
    """Reviewer must use temperature 0.1."""
    from selfmod.reviewer import REVIEWER_TEMPERATURE
    assert REVIEWER_TEMPERATURE == 0.1


def test_auditor_temperature():
    """Auditor must use temperature 0.0."""
    from selfmod.auditor import AUDITOR_TEMPERATURE
    assert AUDITOR_TEMPERATURE == 0.0


def test_selfmod_model_config():
    """CLIVE_SELFMOD_MODEL env var must be respected."""
    from selfmod.reviewer import get_selfmod_model
    os.environ["CLIVE_SELFMOD_MODEL"] = "test-model-123"
    try:
        assert get_selfmod_model() == "test-model-123"
    finally:
        del os.environ["CLIVE_SELFMOD_MODEL"]


def test_selfmod_model_default():
    """Without env var, selfmod model should return None (use default)."""
    from selfmod.reviewer import get_selfmod_model
    os.environ.pop("CLIVE_SELFMOD_MODEL", None)
    assert get_selfmod_model() is None
