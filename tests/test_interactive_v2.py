# tests/test_interactive_v2.py
"""Tests for the refocused interactive worker."""
import pytest
from unittest.mock import MagicMock, patch
from models import Subtask, SubtaskResult, SubtaskStatus, PaneInfo


def _make_pane_info():
    pane = MagicMock()
    pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
    return PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")


def _make_subtask(**kw):
    defaults = dict(id="1", description="list files", pane="shell", mode="interactive", max_turns=5)
    defaults.update(kw)
    return Subtask(**defaults)


class TestInteractiveV2:
    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    def test_single_command_then_done(self, mock_wait, mock_capture, mock_chat, mock_stream):
        """LLM sends a command, then DONE on next turn."""
        mock_capture.side_effect = [
            "[AGENT_READY] $ ",  # turn 1: initial screen
            "file1.txt\nfile2.txt\n[AGENT_READY] $ ",  # turn 2: after ls
        ]
        mock_chat.side_effect = [
            ("```bash\nls\n```", 100, 50),  # turn 1: command
            ("DONE: found 2 files", 100, 30),  # turn 2: done
        ]
        mock_wait.return_value = ("file1.txt\nfile2.txt\n[AGENT_READY] $ ", "marker")

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
        assert "2 files" in result.summary

    @patch("interactive_runner.chat_stream")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    def test_streaming_happy_path(self, mock_wait, mock_capture, mock_stream):
        """chat_stream succeeds — detector fires during streaming."""
        mock_capture.side_effect = [
            "[AGENT_READY] $ ",
            "file1.txt\n[AGENT_READY] $ ",
        ]

        def fake_stream(client, messages, on_token=None):
            text = "```bash\nls\n```"
            if on_token:
                for i in range(1, len(text) + 1):
                    on_token(text[:i])
            return text, 100, 50

        mock_stream.side_effect = [
            fake_stream(None, []),
            ("DONE: found files", 50, 20),
        ]
        mock_wait.return_value = ("file1.txt\n[AGENT_READY] $ ", "marker")

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
        assert "files" in result.summary.lower()

    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    def test_done_on_first_reply(self, mock_capture, mock_chat, mock_stream):
        """LLM immediately says DONE (trivial task)."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_chat.return_value = ("DONE: nothing to do", 50, 20)

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED

    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    def test_exhausted_turns(self, mock_capture, mock_chat, mock_stream):
        """Worker exhausts turns without DONE."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_chat.return_value = ("```bash\nls\n```", 100, 50)

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(max_turns=2),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.FAILED
        assert "turns" in result.summary.lower()

    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    def test_blocked_command(self, mock_capture, mock_chat, mock_stream):
        """Dangerous command gets blocked, worker continues."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_chat.side_effect = [
            ("```bash\nrm -rf /\n```", 100, 50),  # dangerous
            ("DONE: aborted", 50, 20),
        ]

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(max_turns=3),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
