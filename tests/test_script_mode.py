"""Tests for script mode prompt generation."""
from prompts import build_script_prompt


def test_build_script_prompt_contains_task():
    prompt = build_script_prompt(
        subtask_description="Count lines in /tmp/test.txt",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="",
        session_dir="/tmp/clive/abc123",
    )
    assert "Count lines" in prompt
    assert "/tmp/clive/abc123" in prompt
    assert "script" in prompt.lower()


def test_build_script_prompt_with_deps():
    prompt = build_script_prompt(
        subtask_description="Summarize results",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="[Subtask 1 result]: Found 42 files",
        session_dir="/tmp/clive/abc123",
    )
    assert "Found 42 files" in prompt
