"""Tests for dynamic agent pane injection."""
from session import ensure_agent_pane


def test_ensure_agent_pane_returns_pane_def():
    """Verify ensure_agent_pane returns the right structure (unit test only, no tmux)."""
    from unittest.mock import MagicMock, patch
    from models import PaneInfo

    mock_session = MagicMock()
    mock_window = MagicMock()
    mock_pane_obj = MagicMock()
    mock_session.new_window.return_value = mock_window
    mock_window.active_pane = mock_pane_obj
    mock_pane_obj.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])

    panes = {"shell": MagicMock(spec=PaneInfo)}
    config = {
        "cmd": "ssh localhost 'python3 clive.py --conversational'",
        "host": "localhost",
        "connect_timeout": 1,
        "app_type": "agent",
    }

    with patch("time.sleep"):
        result = ensure_agent_pane(mock_session, panes, "localhost", config)

    assert "agent-localhost" in panes
    assert isinstance(result, PaneInfo)
    assert result.app_type == "agent"
    assert result.name == "agent-localhost"


def test_ensure_agent_pane_reuses_existing():
    """If pane already exists, return it without creating a new one."""
    from unittest.mock import MagicMock
    from models import PaneInfo

    mock_session = MagicMock()
    existing_pane = MagicMock(spec=PaneInfo)
    existing_pane.app_type = "agent"
    existing_pane.name = "agent-localhost"
    panes = {"agent-localhost": existing_pane}

    result = ensure_agent_pane(mock_session, panes, "localhost", {})
    assert result is existing_pane
    mock_session.new_window.assert_not_called()
