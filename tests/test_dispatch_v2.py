# tests/test_dispatch_v2.py
"""Verify run_subtask dispatches interactive mode to v2 worker."""
from unittest.mock import patch, MagicMock
from models import Subtask, SubtaskStatus, PaneInfo


def test_interactive_dispatches_to_v2():
    """Interactive mode should use run_subtask_interactive."""
    subtask = Subtask(id="1", description="test", pane="shell", mode="interactive")
    pane_info = PaneInfo(
        pane=MagicMock(), app_type="shell", description="Bash", name="shell"
    )

    with patch("executor.run_subtask_interactive") as mock_v2:
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
