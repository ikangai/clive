"""Tests for eval verifiers."""
import os
import json
import tempfile
from evals.harness.verifier import DeterministicVerifier, verify_task


def test_deterministic_verifier_pass(tmp_path):
    result_file = tmp_path / "result.txt"
    result_file.write_text("hello world\n")
    v = DeterministicVerifier(
        check=f'grep -q "hello" {result_file}',
        workdir=str(tmp_path),
    )
    assert v.verify() is True


def test_deterministic_verifier_fail(tmp_path):
    result_file = tmp_path / "result.txt"
    result_file.write_text("goodbye\n")
    v = DeterministicVerifier(
        check=f'grep -q "hello" {result_file}',
        workdir=str(tmp_path),
    )
    assert v.verify() is False


def test_verify_task_deterministic(tmp_path):
    result_file = tmp_path / "output.txt"
    result_file.write_text("42\n")
    task_def = {
        "success_criteria": {
            "type": "deterministic",
            "check": f'test "$(cat {result_file})" = "42"',
        }
    }
    passed, detail = verify_task(task_def, workdir=str(tmp_path))
    assert passed is True
