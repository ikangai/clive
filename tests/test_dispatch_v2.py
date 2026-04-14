# tests/test_dispatch_v2.py
"""Verify run_subtask dispatches interactive mode correctly.

Interactive mode now tries the tool-calling runner first (for supported
providers), then falls back to the text-based interactive runner.
"""
from unittest.mock import patch, MagicMock
from models import Subtask, SubtaskStatus, PaneInfo


def test_interactive_tries_toolcall_first():
    """Interactive mode should try run_subtask_toolcall for supported providers."""
    subtask = Subtask(id="1", description="test", pane="shell", mode="interactive")
    pane_info = PaneInfo(
        pane=MagicMock(), app_type="shell", description="Bash", name="shell"
    )

    with patch("toolcall_runner.run_subtask_toolcall") as mock_tc:
        mock_tc.return_value = MagicMock(status=SubtaskStatus.COMPLETED)
        from executor import run_subtask
        run_subtask(subtask=subtask, pane_info=pane_info, dep_context="")
        mock_tc.assert_called_once()


def test_interactive_falls_back_on_toolcall_failure():
    """If toolcall runner raises, fall back to text-based interactive runner."""
    subtask = Subtask(id="1", description="test", pane="shell", mode="interactive")
    pane_info = PaneInfo(
        pane=MagicMock(), app_type="shell", description="Bash", name="shell"
    )

    with (
        patch("toolcall_runner.run_subtask_toolcall", side_effect=RuntimeError("unsupported")),
        patch("executor.run_subtask_interactive") as mock_v2,
    ):
        mock_v2.return_value = MagicMock(status=SubtaskStatus.COMPLETED)
        from executor import run_subtask
        run_subtask(subtask=subtask, pane_info=pane_info, dep_context="")
        mock_v2.assert_called_once()


def test_script_still_works():
    """Script mode should NOT use v2 worker."""
    subtask = Subtask(id="1", description="test", pane="shell", mode="script")
    pane_info = PaneInfo(
        pane=MagicMock(), app_type="shell", description="Bash", name="shell"
    )

    with patch("executor.run_subtask_script") as mock_script:
        mock_script.return_value = MagicMock(status=SubtaskStatus.COMPLETED)
        from executor import run_subtask
        run_subtask(subtask=subtask, pane_info=pane_info, dep_context="")
        mock_script.assert_called_once()
