"""Tests for executable skill runner."""
from skill_runner import parse_executable_steps


def test_parse_steps_from_skill():
    content = """# Test Skill

STEPS:
- cmd: echo hello
  check: exit_code 0
  on_fail: abort
- cmd: cat file.txt
  check: file_exists /tmp/output.txt
  on_fail: skip
- cmd: curl -s URL
  check: valid_json
  save: response.json
"""
    steps = parse_executable_steps(content)
    assert len(steps) == 3
    assert steps[0]["cmd"] == "echo hello"
    assert steps[0]["check_type"] == "exit_code"
    assert steps[0]["check_value"] == "0"
    assert steps[0]["on_fail"] == "abort"
    assert steps[1]["check_type"] == "file_exists"
    assert steps[1]["on_fail"] == "skip"
    assert steps[2]["check_type"] == "valid_json"
    assert steps[2]["save"] == "response.json"


def test_parse_no_steps():
    content = """# Prose Skill

PROCEDURE:
1. Do this
2. Do that
"""
    steps = parse_executable_steps(content)
    assert steps == []


def test_parse_output_contains():
    content = """STEPS:
- cmd: curl -sI URL
  check: output_contains 200
  on_fail: abort
"""
    steps = parse_executable_steps(content)
    assert len(steps) == 1
    assert steps[0]["check_type"] == "output_contains"
    assert steps[0]["check_value"] == "200"


def test_parse_real_skill():
    from skills import load_skill
    skill = load_skill("api-health")
    assert skill is not None
    steps = parse_executable_steps(skill)
    assert len(steps) == 3
    assert "{URL}" in steps[0]["cmd"]
    assert steps[0]["check_type"] == "output_contains"


def test_default_on_fail():
    content = """STEPS:
- cmd: echo test
  check: exit_code 0
"""
    steps = parse_executable_steps(content)
    assert steps[0]["on_fail"] == "abort"  # default
