"""Tests for observation-action decoupling in the interactive runner.

Verifies that the observation classifier injects compact event messages
when a command succeeds (exit_code==0, needs_llm==False), and that
error/intervention handling is unaffected.
"""

import sys, os

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


class TestObservationDecoupling:
    """Observation classifier integration in the interactive loop."""

    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    def test_compact_event_injected_on_success(self, mock_wait, mock_capture, mock_chat, mock_stream):
        """When exit_code==0 and needs_llm==False, a compact event message is appended."""
        mock_capture.side_effect = [
            "[AGENT_READY] $ ",                    # turn 1: initial screen
            "file1.txt\nfile2.txt\nEXIT:0 ___DONE_1\n[AGENT_READY] $ ",  # turn 2: after ls
        ]
        mock_chat.side_effect = [
            ("```bash\nls\n```", 100, 50),        # turn 1: command
            ("DONE: found 2 files", 100, 30),      # turn 2: done
        ]
        # wait_for_ready returns screen with exit marker
        mock_wait.return_value = ("file1.txt\nfile2.txt\nEXIT:0 ___DONE_1\n[AGENT_READY] $ ", "marker")

        from interactive_runner import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED

        # Verify that the compact event was in the messages passed to the LLM
        # The second chat call's messages should contain [OK exit:0]
        second_call_messages = mock_chat.call_args_list[1][0][1]  # positional arg 1
        user_messages = [m["content"] for m in second_call_messages if m["role"] == "user"]
        assert any("[OK exit:0]" in msg for msg in user_messages), \
            f"Expected compact event in messages, got: {user_messages}"

    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    def test_error_handling_unaffected(self, mock_wait, mock_capture, mock_chat, mock_stream):
        """When exit_code!=0, normal error handling fires (observation doesn't interfere)."""
        mock_capture.side_effect = [
            "[AGENT_READY] $ ",                    # turn 1
            "error output\nEXIT:1 ___DONE_1\n[AGENT_READY] $ ",  # turn 2
        ]
        mock_chat.side_effect = [
            ("```bash\nfalse\n```", 100, 50),      # turn 1: failing command
            ("DONE: command failed", 100, 30),      # turn 2: done
        ]
        mock_wait.return_value = ("error output\nEXIT:1 ___DONE_1\n[AGENT_READY] $ ", "marker")

        from interactive_runner import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED

        # Should have the error injection, NOT a compact event
        second_call_messages = mock_chat.call_args_list[1][0][1]
        user_messages = [m["content"] for m in second_call_messages if m["role"] == "user"]
        assert any("[EXIT:1]" in msg for msg in user_messages), \
            f"Expected error injection in messages, got: {user_messages}"
        assert not any("[OK exit:0]" in msg for msg in user_messages), \
            "Compact event should NOT appear for error cases"

    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    def test_intervention_suppresses_compact_event(self, mock_wait, mock_capture, mock_chat, mock_stream):
        """When intervention is detected, compact event is NOT injected even with exit_code==0."""
        mock_capture.side_effect = [
            "[AGENT_READY] $ ",
            "Do you want to proceed? [Y/n] \nEXIT:0 ___DONE_1",
        ]
        mock_chat.side_effect = [
            ("```bash\nsome-command\n```", 100, 50),
            ("DONE: answered prompt", 100, 30),
        ]
        mock_wait.return_value = (
            "Do you want to proceed? [Y/n] \nEXIT:0 ___DONE_1",
            "intervention:confirmation_prompt",
        )

        from interactive_runner import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED

        # Should have intervention message, NOT compact event
        second_call_messages = mock_chat.call_args_list[1][0][1]
        user_messages = [m["content"] for m in second_call_messages if m["role"] == "user"]
        assert any("[INTERVENTION:" in msg for msg in user_messages)
        assert not any("[OK exit:0]" in msg for msg in user_messages)

    def test_compact_event_shorter_than_diff(self):
        """The compact event format is shorter than a raw screen diff."""
        from observation import ScreenClassifier, format_event_for_llm
        from screen_diff import compute_screen_diff

        screen_before = "[AGENT_READY] $ "
        screen_after = (
            "total 24\n"
            "drwxr-xr-x  5 user staff  160 Apr 14 10:00 .\n"
            "drwxr-xr-x  3 user staff   96 Apr 14 09:00 ..\n"
            "-rw-r--r--  1 user staff 1234 Apr 14 10:00 file1.txt\n"
            "-rw-r--r--  1 user staff 5678 Apr 14 10:00 file2.txt\n"
            "-rw-r--r--  1 user staff 9012 Apr 14 10:00 file3.txt\n"
            "EXIT:0 ___DONE_1\n"
            "[AGENT_READY] $ "
        )

        diff = compute_screen_diff(screen_before, screen_after)

        classifier = ScreenClassifier()
        event = classifier.classify(screen_after, exit_code=0)
        compact = format_event_for_llm(event)

        assert len(compact) < len(diff), \
            f"Compact ({len(compact)} chars) should be shorter than diff ({len(diff)} chars)"
