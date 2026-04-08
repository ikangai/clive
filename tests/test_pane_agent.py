"""Tests for PaneAgent and SharedBrain."""
from unittest.mock import MagicMock
from pane_agent import PaneAgent, SharedBrain, _extract_task_pattern
from models import PaneInfo, SubtaskResult, SubtaskStatus, Subtask


def _make_agent(brain=None):
    mock_pane = MagicMock()
    info = PaneInfo(pane=mock_pane, app_type="shell", description="test", name="shell")
    return PaneAgent(info, session_dir="/tmp/test", shared_brain=brain)


# ─── SharedBrain ──────────────────────────────────────────────────────────────

def test_shared_brain_facts():
    brain = SharedBrain("/tmp/test")
    brain.post_fact("shell", "API needs auth token")
    brain.post_fact("browser", "Website uses HTTPS")
    assert len(brain.facts) == 2
    ctx = brain.get_context_for_agent("data")
    assert "API needs auth" in ctx
    assert "Website uses HTTPS" in ctx


def test_shared_brain_direct_messages():
    brain = SharedBrain("/tmp/test")
    brain.send_message("shell", "browser", "Check example.com/api")
    msgs = brain.get_messages("browser")
    assert len(msgs) == 1
    assert msgs[0]["from"] == "shell"
    assert "example.com" in msgs[0]["message"]
    # Messages are consumed
    assert brain.get_messages("browser") == []


def test_shared_brain_delegation():
    brain = SharedBrain("/tmp/test")
    brain.request_work("shell", "browser", "fetch the API docs")
    work = brain.get_delegated_work("browser")
    assert len(work) == 1
    assert work[0]["from"] == "shell"
    # Work is consumed
    assert brain.get_delegated_work("browser") == []


def test_shared_brain_persistence(tmp_path):
    brain = SharedBrain(str(tmp_path))
    brain.post_fact("shell", "important finding")
    path = str(tmp_path / "brain.json")
    brain.save(path)

    loaded = SharedBrain.load(path, str(tmp_path))
    assert len(loaded.facts) == 1
    assert "important finding" in loaded.facts[0]["fact"]


def test_shared_brain_context():
    brain = SharedBrain("/tmp/test")
    brain.post_fact("shell", "found API key")
    brain.send_message("shell", "browser", "use Bearer auth")
    brain.request_work("shell", "browser", "fetch the data")

    ctx = brain.get_context_for_agent("browser")
    assert "Shared knowledge" in ctx
    assert "Messages for you" in ctx
    assert "Work requests" in ctx


# ─── PaneAgent basics ─────────────────────────────────────────────────────────

def test_creation():
    agent = _make_agent()
    assert agent.name == "shell"
    assert agent.memory == []
    assert agent.shortcuts == {}
    assert agent.success_rate == 1.0


def test_repr():
    agent = _make_agent()
    assert "PaneAgent" in repr(agent)


# ─── Memory ──────────────────────────────────────────────────────────────────

def test_memory_on_success():
    agent = _make_agent()
    s = Subtask(id="1", description="extract data", pane="shell", mode="script")
    r = SubtaskResult(subtask_id="1", status=SubtaskStatus.COMPLETED,
                      summary="Extracted 47 rows", output_snippet="done",
                      turns_used=1, prompt_tokens=500, completion_tokens=200)
    agent._update_after_subtask(s, r)
    assert len(agent.memory) == 1
    assert "47 rows" in agent.memory[0]


def test_failure_memory():
    agent = _make_agent()
    s = Subtask(id="1", description="fail", pane="shell")
    r = SubtaskResult(subtask_id="1", status=SubtaskStatus.FAILED,
                      summary="Command not found", output_snippet="",
                      error="bash: xyz: not found", turns_used=3,
                      prompt_tokens=1000, completion_tokens=500)
    agent._update_after_subtask(s, r)
    assert any("FAILED" in m for m in agent.memory)


# ─── Shared brain integration ────────────────────────────────────────────────

def test_agent_posts_to_shared_brain():
    brain = SharedBrain("/tmp/test")
    agent = _make_agent(brain)
    s = Subtask(id="1", description="find files", pane="shell", mode="script")
    r = SubtaskResult(subtask_id="1", status=SubtaskStatus.COMPLETED,
                      summary="Found 3 files", output_snippet="a.txt\nb.txt",
                      turns_used=1, prompt_tokens=500, completion_tokens=200)
    agent._update_after_subtask(s, r)
    assert len(brain.facts) == 1
    assert "Found 3 files" in brain.facts[0]["fact"]


def test_agent_sees_shared_context():
    brain = SharedBrain("/tmp/test")
    brain.post_fact("browser", "API key is XYZ")
    agent = _make_agent(brain)
    ctx = agent._build_pane_context()
    assert "API key is XYZ" in ctx


# ─── Self-adaptation ─────────────────────────────────────────────────────────

def test_escalate_to_interactive_after_failures():
    agent = _make_agent()
    agent.subtasks_failed = ["1", "2"]
    s = Subtask(id="3", description="try again", pane="shell", mode="script")
    agent._adapt_before_subtask(s)
    assert s.mode == "interactive"


def test_boost_turns_on_low_success():
    agent = _make_agent()
    agent.subtasks_completed = ["1"]
    agent.subtasks_failed = ["2", "3", "4"]
    s = Subtask(id="5", description="retry", pane="shell", max_turns=5)
    agent._adapt_before_subtask(s)
    assert s.max_turns >= 12


def test_no_adaptation_when_healthy():
    agent = _make_agent()
    agent.subtasks_completed = ["1", "2", "3"]
    s = Subtask(id="4", description="next", pane="shell", mode="script", max_turns=5)
    agent._adapt_before_subtask(s)
    assert s.mode == "script"  # not escalated
    assert s.max_turns == 5  # not boosted


# ─── Persistence ──────────────────────────────────────────────────────────────

def test_save_and_load(tmp_path):
    agent = _make_agent()
    agent.memory = ["(1) found files", "(2) processed data"]
    agent.shortcuts = {"extract data": "script mode, 1 attempt"}
    path = str(tmp_path / "shell.json")
    agent.save(path)

    agent2 = _make_agent()
    agent2.load_state(path)
    assert agent2.memory == ["(1) found files", "(2) processed data"]
    assert "extract data" in agent2.shortcuts


def test_load_missing_file():
    agent = _make_agent()
    agent.load_state("/nonexistent/path.json")
    assert agent.memory == []  # graceful, no crash


# ─── Task pattern extraction ─────────────────────────────────────────────────

def test_extract_pattern():
    assert _extract_task_pattern("extract names from data.json") == "extract names from"


def test_extract_pattern_empty():
    assert _extract_task_pattern("") == ""


# ─── Toolsets ────────────────────────────────────────────────────────────────

def test_agent_pane_in_toolsets():
    from toolsets import PANES
    assert "remote_agent" in PANES
    assert PANES["remote_agent"]["app_type"] == "agent"
