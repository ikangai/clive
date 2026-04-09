# tests/test_selfmod_eval_gate.py
"""Tests for eval-gated self-modification."""

from selfmod.eval_gate import identify_affected_evals, EvalGateResult, check_eval_gate

def test_identify_affected_evals_executor():
    """Changes to executor.py should trigger layer 2+3 evals."""
    files = ["executor.py"]
    affected = identify_affected_evals(files)
    assert "layer2" in affected or "layer3" in affected

def test_identify_affected_evals_planner():
    """Changes to planner.py should trigger layer 2 evals."""
    files = ["planner.py"]
    affected = identify_affected_evals(files)
    assert "layer2" in affected

def test_identify_affected_evals_llm():
    """Changes to llm.py should trigger all evals."""
    files = ["llm.py"]
    affected = identify_affected_evals(files)
    assert len(affected) >= 2

def test_identify_affected_evals_unknown_file():
    """Unknown files should trigger layer 2 as default."""
    files = ["some_new_file.py"]
    affected = identify_affected_evals(files)
    assert "layer2" in affected

def test_eval_gate_result_pass():
    """EvalGateResult with no regression should pass."""
    result = EvalGateResult(passed=True, message="No regression", baseline_score=0.8, new_score=0.85)
    assert result.passed

def test_eval_gate_result_fail():
    """EvalGateResult with regression should fail."""
    result = EvalGateResult(passed=False, message="Regression detected", baseline_score=0.8, new_score=0.6)
    assert not result.passed

def test_check_eval_gate_dry_run():
    """Dry run should always pass without running evals."""
    result = check_eval_gate(["executor.py"], dry_run=True)
    assert result.passed
    assert "dry" in result.message.lower()
