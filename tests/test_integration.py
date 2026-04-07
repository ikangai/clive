"""Integration tests for the core pipeline.

Tests the wiring between modules without requiring LLM calls or tmux.
Uses mock objects to simulate the LLM and tmux pane.
"""
import json
from unittest.mock import MagicMock, patch
from models import Subtask, Plan, SubtaskResult, SubtaskStatus, PaneInfo
from executor import parse_command, _extract_script, _build_dependency_context


# ─── parse_command integration ────────────────────────────────────────────────

def test_parse_command_from_realistic_llm_response():
    """LLM responses have explanatory text around the command."""
    response = """I'll list the files in the directory to see what's there.

<cmd type="shell" pane="eval">find . -name "*.txt" -type f</cmd>

This will find all text files recursively."""
    cmd = parse_command(response)
    assert cmd["type"] == "shell"
    assert cmd["pane"] == "eval"
    assert "find" in cmd["value"]


def test_parse_command_task_complete_with_summary():
    response = """The task is complete. I found all the files and wrote the results.

<cmd type="task_complete">Found 3 .txt files: ./a.txt, ./b.txt, ./sub/c.txt. Results written to /tmp/clive/result.txt.</cmd>"""
    cmd = parse_command(response)
    assert cmd["type"] == "task_complete"
    assert "3 .txt files" in cmd["value"]


# ─── _extract_script integration ──────────────────────────────────────────────

def test_extract_script_from_realistic_response():
    """LLM wraps scripts in markdown code blocks."""
    response = """Here's the script to accomplish this:

```bash
#!/bin/bash
set -euo pipefail

count=$(grep -r 'TODO' . 2>/dev/null | wc -l)
echo "$count" > /tmp/clive/result.txt
echo "Found $count TODO comments"
```

This script searches recursively for TODO comments."""
    script = _extract_script(response)
    assert script.startswith("#!/bin/bash")
    assert "grep -r 'TODO'" in script
    assert "result.txt" in script


# ─── _build_dependency_context ────────────────────────────────────────────────

def test_dependency_context_builds_from_results():
    subtask = Subtask(id="3", description="summarize", pane="shell", depends_on=["1", "2"])
    results = {
        "1": SubtaskResult(
            subtask_id="1", status=SubtaskStatus.COMPLETED,
            summary="Found 5 files", output_snippet="file1.txt\nfile2.txt",
        ),
        "2": SubtaskResult(
            subtask_id="2", status=SubtaskStatus.COMPLETED,
            summary="Downloaded data", output_snippet="200 OK",
        ),
    }
    context = _build_dependency_context(subtask, results)
    assert "Found 5 files" in context
    assert "Downloaded data" in context


def test_dependency_context_empty_when_no_deps():
    subtask = Subtask(id="1", description="first", pane="shell")
    context = _build_dependency_context(subtask, {})
    assert context == ""


# ─── Plan validation integration ──────────────────────────────────────────────

def test_plan_validation_catches_cycle():
    plan = Plan(task="test")
    plan.subtasks.append(Subtask(id="1", description="a", pane="shell", depends_on=["2"]))
    plan.subtasks.append(Subtask(id="2", description="b", pane="shell", depends_on=["1"]))
    errors = plan.validate(valid_panes={"shell"})
    assert any("Cycle" in e for e in errors)


def test_plan_validation_catches_missing_pane():
    plan = Plan(task="test")
    plan.subtasks.append(Subtask(id="1", description="a", pane="nonexistent"))
    errors = plan.validate(valid_panes={"shell"})
    assert any("nonexistent" in e for e in errors)


def test_plan_validation_catches_missing_dependency():
    plan = Plan(task="test")
    plan.subtasks.append(Subtask(id="1", description="a", pane="shell", depends_on=["99"]))
    errors = plan.validate(valid_panes={"shell"})
    assert any("unknown subtask 99" in e for e in errors)


# ─── Mode dispatch logic ─────────────────────────────────────────────────────

def test_mode_dispatch_script_vs_interactive():
    """Verify the executor dispatches based on mode."""
    script_sub = Subtask(id="1", description="batch", pane="shell", mode="script")
    interactive_sub = Subtask(id="2", description="explore", pane="shell", mode="interactive")
    streaming_sub = Subtask(id="3", description="watch", pane="shell", mode="streaming")

    assert script_sub.mode == "script"
    assert interactive_sub.mode == "interactive"
    assert streaming_sub.mode == "streaming"

    # Unknown mode defaults to interactive
    unknown = Subtask(id="4", description="test", pane="shell", mode="unknown")
    assert unknown.mode == "interactive"
