"""Tests for script mode prompt generation and model configuration."""
import sys
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


def test_script_prompt_contains_os_info():
    import platform
    prompt = build_script_prompt(
        subtask_description="List files",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="",
        session_dir="/tmp/clive/test",
    )
    assert platform.system() in prompt
    assert "OS:" in prompt


def test_script_prompt_macos_warning():
    """On macOS, the prompt should warn about GNU vs BSD differences."""
    import platform
    prompt = build_script_prompt(
        subtask_description="Find files",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="",
        session_dir="/tmp/clive/test",
    )
    if platform.system() == "Darwin":
        assert "BSD" in prompt


def test_script_prompt_strict_format_instruction():
    prompt = build_script_prompt(
        subtask_description="Count files",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="",
        session_dir="/tmp/clive/test",
    )
    assert "ONLY" in prompt
    assert "fenced code block" in prompt.lower()
    assert "no prose" in prompt.lower()


def test_script_model_env_var(monkeypatch):
    """SCRIPT_MODEL env var should be exposed from llm module."""
    monkeypatch.setenv("SCRIPT_MODEL", "fast-model-123")
    import importlib
    import llm
    importlib.reload(llm)
    assert llm.SCRIPT_MODEL == "fast-model-123"
    # Cleanup
    monkeypatch.delenv("SCRIPT_MODEL", raising=False)
    importlib.reload(llm)


def test_script_model_defaults_to_agent_model(monkeypatch):
    """When SCRIPT_MODEL is not set, it should equal MODEL."""
    monkeypatch.delenv("SCRIPT_MODEL", raising=False)
    import importlib
    import llm
    importlib.reload(llm)
    assert llm.SCRIPT_MODEL == llm.MODEL
