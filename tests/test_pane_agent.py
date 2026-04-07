"""Tests for PaneAgent — per-pane persistent agent."""
from pane_agent import PaneAgent


def test_pane_agent_creation():
    """PaneAgent can be created without a real pane (for unit testing)."""
    from unittest.mock import MagicMock
    from models import PaneInfo
    mock_pane = MagicMock()
    info = PaneInfo(pane=mock_pane, app_type="shell", description="test", name="shell")
    agent = PaneAgent(info, session_dir="/tmp/test")
    assert agent.name == "shell"
    assert agent.app_type == "shell"
    assert agent.subtasks_completed == []
    assert agent.total_tokens == 0


def test_pane_agent_repr():
    from unittest.mock import MagicMock
    from models import PaneInfo
    mock_pane = MagicMock()
    info = PaneInfo(pane=mock_pane, app_type="shell", description="test", name="shell")
    agent = PaneAgent(info)
    r = repr(agent)
    assert "PaneAgent" in r
    assert "shell" in r
    assert "completed=0" in r


def test_cross_machine_agent_pane_in_toolsets():
    """The remote_agent pane definition exists in toolsets."""
    from toolsets import PANES
    assert "remote_agent" in PANES
    agent_pane = PANES["remote_agent"]
    assert agent_pane["app_type"] == "agent"
    assert "clive" in agent_pane["cmd"]
