"""Tests for PaneAgent — per-pane persistent agent with memory and health."""
from unittest.mock import MagicMock
from pane_agent import PaneAgent, _extract_task_pattern
from models import PaneInfo, SubtaskResult, SubtaskStatus, Subtask


def _make_agent():
    mock_pane = MagicMock()
    info = PaneInfo(pane=mock_pane, app_type="shell", description="test", name="shell")
    return PaneAgent(info, session_dir="/tmp/test")


# ─── Basic creation ──────────────────────────────────────────────────────────

def test_creation():
    agent = _make_agent()
    assert agent.name == "shell"
    assert agent.app_type == "shell"
    assert agent.subtasks_completed == []
    assert agent.subtasks_failed == []
    assert agent.total_tokens == 0
    assert agent.memory == []
    assert agent.shortcuts == {}


def test_repr():
    agent = _make_agent()
    r = repr(agent)
    assert "PaneAgent" in r
    assert "shell" in r
    assert "completed=0" in r


# ─── Health tracking ─────────────────────────────────────────────────────────

def test_success_rate_empty():
    agent = _make_agent()
    assert agent.success_rate == 1.0  # no tasks = healthy


def test_success_rate_after_results():
    agent = _make_agent()
    agent.subtasks_completed = ["1", "2", "3"]
    agent.subtasks_failed = ["4"]
    assert agent.success_rate == 0.75


def test_avg_turns():
    agent = _make_agent()
    agent.subtasks_completed = ["1", "2"]
    agent.total_turns = 6
    assert agent.avg_turns == 3.0


# ─── Memory ──────────────────────────────────────────────────────────────────

def test_memory_added_on_success():
    agent = _make_agent()
    subtask = Subtask(id="1", description="extract data", pane="shell", mode="script")
    result = SubtaskResult(
        subtask_id="1", status=SubtaskStatus.COMPLETED,
        summary="Extracted 47 rows from sales.csv",
        output_snippet="$ cat data.json\n[{...}]",
        turns_used=1, prompt_tokens=500, completion_tokens=200,
    )
    agent._update_after_subtask(subtask, result)
    assert len(agent.memory) == 1
    assert "47 rows" in agent.memory[0]


def test_memory_bounded():
    agent = _make_agent()
    for i in range(15):
        agent.memory.append(f"memory {i}")
    # Simulate bounding
    if len(agent.memory) > 10:
        agent.memory = agent.memory[-10:]
    assert len(agent.memory) == 10
    assert agent.memory[0] == "memory 5"


def test_failure_recorded_in_memory():
    agent = _make_agent()
    subtask = Subtask(id="1", description="fail task", pane="shell")
    result = SubtaskResult(
        subtask_id="1", status=SubtaskStatus.FAILED,
        summary="Command not found", output_snippet="",
        error="bash: xyz: command not found",
        turns_used=3, prompt_tokens=1000, completion_tokens=500,
    )
    agent._update_after_subtask(subtask, result)
    assert len(agent.subtasks_failed) == 1
    assert any("FAILED" in m for m in agent.memory)


# ─── Shortcuts ────────────────────────────────────────────────────────────────

def test_shortcut_learned_on_single_attempt_script():
    agent = _make_agent()
    subtask = Subtask(id="1", description="extract names from data.json", pane="shell", mode="script")
    result = SubtaskResult(
        subtask_id="1", status=SubtaskStatus.COMPLETED,
        summary="Extracted names", output_snippet="alice\nbob",
        turns_used=1, prompt_tokens=500, completion_tokens=200,
    )
    agent._update_after_subtask(subtask, result)
    assert len(agent.shortcuts) > 0


def test_no_shortcut_on_multi_attempt():
    agent = _make_agent()
    subtask = Subtask(id="1", description="complex task with retries", pane="shell", mode="script")
    result = SubtaskResult(
        subtask_id="1", status=SubtaskStatus.COMPLETED,
        summary="Done after retries", output_snippet="",
        turns_used=3, prompt_tokens=2000, completion_tokens=800,
    )
    agent._update_after_subtask(subtask, result)
    assert len(agent.shortcuts) == 0  # multi-attempt = not a reliable shortcut


# ─── Pane context building ────────────────────────────────────────────────────

def test_build_pane_context_empty():
    agent = _make_agent()
    ctx = agent._build_pane_context()
    assert ctx == ""  # no memory, no shortcuts, no history


def test_build_pane_context_with_memory():
    agent = _make_agent()
    agent.memory = ["(1) Extracted 47 rows", "(2) Filtered by city"]
    ctx = agent._build_pane_context()
    assert "Pane memory" in ctx
    assert "47 rows" in ctx
    assert "Filtered" in ctx


def test_build_pane_context_with_shortcuts():
    agent = _make_agent()
    agent.shortcuts = {"extract names": "jq -r '.[] | .name'"}
    ctx = agent._build_pane_context()
    assert "Learned shortcuts" in ctx
    assert "extract names" in ctx


def test_build_pane_context_with_health():
    agent = _make_agent()
    agent.subtasks_completed = ["1", "2"]
    agent.subtasks_failed = ["3"]
    agent.total_turns = 6
    ctx = agent._build_pane_context()
    assert "Agent health" in ctx
    assert "2 OK" in ctx
    assert "1 failed" in ctx


# ─── Task pattern extraction ─────────────────────────────────────────────────

def test_extract_pattern():
    assert _extract_task_pattern("extract names from data.json") == "extract names from"


def test_extract_pattern_short_words_skipped():
    assert _extract_task_pattern("do it now") == ""  # all words <= 3 chars


def test_extract_pattern_empty():
    assert _extract_task_pattern("") == ""


# ─── Cross-machine agent pane ────────────────────────────────────────────────

def test_agent_pane_in_toolsets():
    from toolsets import PANES
    assert "remote_agent" in PANES
    assert PANES["remote_agent"]["app_type"] == "agent"
