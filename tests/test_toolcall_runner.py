"""Tests for the tool-calling interactive runner (toolcall_runner.py)."""

import threading
import types
from unittest.mock import MagicMock, patch

import pytest

from models import Subtask, SubtaskStatus, PaneInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_subtask(id="t1", description="list files", pane="shell", mode="interactive", max_turns=5):
    return Subtask(id=id, description=description, pane=pane, mode=mode, max_turns=max_turns)


def _make_pane_info():
    pane = MagicMock()
    pane.send_keys = MagicMock()
    return PaneInfo(
        pane=pane,
        name="shell",
        description="shell pane",
        app_type="shell",
        sandboxed=False,
    )


def _openai_tool_call(id, name, arguments):
    """Create a mock OpenAI-style tool_call object."""
    func = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(id=id, function=func)


# ---------------------------------------------------------------------------
# Test 1: Single command then complete → COMPLETED
# ---------------------------------------------------------------------------

class TestSingleCommandThenComplete:
    @patch("toolcall_runner.capture_pane")
    @patch("toolcall_runner.wait_for_ready")
    @patch("toolcall_runner.wrap_command")
    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.get_client")
    def test_single_command_then_complete(self, mock_client, mock_chat, mock_wrap,
                                          mock_wait, mock_capture):
        mock_client.return_value = MagicMock()  # OpenAI-style client

        # Turn 1: run_command("ls")
        # Turn 2: complete
        mock_chat.side_effect = [
            (
                [_openai_tool_call("c1", "run_command", '{"command": "ls -la"}')],
                "Let me list the files.",
                10, 5,
            ),
            (
                [_openai_tool_call("c2", "complete", '{"summary": "Listed files successfully"}')],
                "Done.",
                10, 5,
            ),
        ]

        mock_capture.return_value = "$ \nfile1.txt\nfile2.txt"
        mock_wrap.return_value = ("wrapped_ls", "marker_1")
        mock_wait.return_value = ("$ \nfile1.txt\nfile2.txt\nEXIT:0 ___DONE___", "ready")

        subtask = _make_subtask()
        pane_info = _make_pane_info()

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(subtask, pane_info, dep_context="", session_dir="/tmp/clive")

        assert result.status == SubtaskStatus.COMPLETED
        assert result.summary == "Listed files successfully"
        assert result.turns_used == 2
        assert result.prompt_tokens == 20
        assert result.completion_tokens == 10


# ---------------------------------------------------------------------------
# Test 2: Batched commands (2 run_command in one response)
# ---------------------------------------------------------------------------

class TestBatchedCommands:
    @patch("toolcall_runner.capture_pane")
    @patch("toolcall_runner.wait_for_ready")
    @patch("toolcall_runner.wrap_command")
    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.get_client")
    def test_batched_commands(self, mock_client, mock_chat, mock_wrap,
                               mock_wait, mock_capture):
        mock_client.return_value = MagicMock()

        # Turn 1: two run_commands in batch
        # Turn 2: complete
        mock_chat.side_effect = [
            (
                [
                    _openai_tool_call("c1", "run_command", '{"command": "pwd"}'),
                    _openai_tool_call("c2", "run_command", '{"command": "wc -l file.txt"}'),
                ],
                "Running both commands.",
                15, 8,
            ),
            (
                [_openai_tool_call("c3", "complete", '{"summary": "Checked directory and counted lines"}')],
                "",
                10, 5,
            ),
        ]

        mock_capture.return_value = "$ /home/user"
        mock_wrap.side_effect = [
            ("wrapped_pwd", "marker_1"),
            ("wrapped_wc", "marker_2"),
        ]
        mock_wait.side_effect = [
            ("/home/user\nEXIT:0 ___DONE___", "ready"),
            ("  10 file.txt\nEXIT:0 ___DONE___", "ready"),
        ]

        subtask = _make_subtask()
        pane_info = _make_pane_info()

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(subtask, pane_info, dep_context="", session_dir="/tmp/clive")

        assert result.status == SubtaskStatus.COMPLETED
        # Both commands executed: wait_for_ready called twice
        assert mock_wait.call_count == 2
        # wrap_command called twice
        assert mock_wrap.call_count == 2


# ---------------------------------------------------------------------------
# Test 3: No tool calls, text-based DONE fallback
# ---------------------------------------------------------------------------

class TestTextDoneFallback:
    @patch("toolcall_runner.capture_pane")
    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.get_client")
    def test_done_fallback(self, mock_client, mock_chat, mock_capture):
        mock_client.return_value = MagicMock()

        # LLM returns no tool calls, just text with DONE:
        mock_chat.return_value = (
            [],  # no tool calls
            "DONE: Task completed via text fallback",
            10, 5,
        )

        mock_capture.return_value = "$ ready"

        subtask = _make_subtask()
        pane_info = _make_pane_info()

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(subtask, pane_info, dep_context="", session_dir="/tmp/clive")

        assert result.status == SubtaskStatus.COMPLETED
        assert "text fallback" in result.summary


# ---------------------------------------------------------------------------
# Test 4: Exhausted turns → FAILED
# ---------------------------------------------------------------------------

class TestExhaustedTurns:
    @patch("toolcall_runner.capture_pane")
    @patch("toolcall_runner.wait_for_ready")
    @patch("toolcall_runner.wrap_command")
    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.get_client")
    def test_exhausted_turns(self, mock_client, mock_chat, mock_wrap,
                              mock_wait, mock_capture):
        mock_client.return_value = MagicMock()

        # Every turn: run_command but never complete
        mock_chat.return_value = (
            [_openai_tool_call("c1", "run_command", '{"command": "echo thinking"}')],
            "Still working...",
            10, 5,
        )

        mock_capture.return_value = "$ thinking"
        mock_wrap.return_value = ("wrapped_echo", "marker_1")
        mock_wait.return_value = ("thinking\nEXIT:0 ___DONE___", "ready")

        subtask = _make_subtask(max_turns=3)
        pane_info = _make_pane_info()

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(subtask, pane_info, dep_context="", session_dir="/tmp/clive")

        assert result.status == SubtaskStatus.FAILED
        assert "Exhausted" in result.summary
        assert result.turns_used == 3


# ---------------------------------------------------------------------------
# Test 5: Safety check blocks dangerous command
# ---------------------------------------------------------------------------

class TestSafetyBlock:
    @patch("toolcall_runner.capture_pane")
    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.get_client")
    def test_blocked_command(self, mock_client, mock_chat, mock_capture):
        mock_client.return_value = MagicMock()

        # Turn 1: dangerous command → blocked
        # Turn 2: complete
        mock_chat.side_effect = [
            (
                [_openai_tool_call("c1", "run_command", '{"command": "rm -rf /"}')],
                "Removing everything.",
                10, 5,
            ),
            (
                [_openai_tool_call("c2", "complete", '{"summary": "Aborted dangerous command"}')],
                "",
                10, 5,
            ),
        ]

        mock_capture.return_value = "$ ready"

        subtask = _make_subtask()
        pane_info = _make_pane_info()

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(subtask, pane_info, dep_context="", session_dir="/tmp/clive")

        assert result.status == SubtaskStatus.COMPLETED
        # The dangerous command should NOT have been sent to the pane
        pane_info.pane.send_keys.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: _execute_tool_call handles all three tools
# ---------------------------------------------------------------------------

class TestExecuteToolCall:
    def test_complete_tool(self):
        from toolcall_runner import _execute_tool_call
        tc = {"name": "complete", "args": {"summary": "all done"}, "id": "c1"}
        result = _execute_tool_call(tc, _make_subtask(), _make_pane_info(), "/tmp/clive")
        assert result["type"] == "complete"
        assert result["summary"] == "all done"

    @patch("toolcall_runner.capture_pane")
    def test_read_screen_tool(self, mock_capture):
        mock_capture.return_value = "screen content here"
        from toolcall_runner import _execute_tool_call
        tc = {"name": "read_screen", "args": {"lines": 30}, "id": "c2"}
        result = _execute_tool_call(tc, _make_subtask(), _make_pane_info(), "/tmp/clive")
        assert result["type"] == "screen"
        assert result["content"] == "screen content here"

    @patch("toolcall_runner.wait_for_ready")
    @patch("toolcall_runner.wrap_command")
    def test_run_command_tool(self, mock_wrap, mock_wait):
        mock_wrap.return_value = ("wrapped", "marker")
        mock_wait.return_value = ("output\nEXIT:0 ___DONE___", "ready")

        from toolcall_runner import _execute_tool_call
        pane_info = _make_pane_info()
        tc = {"name": "run_command", "args": {"command": "echo hello"}, "id": "c3"}
        result = _execute_tool_call(tc, _make_subtask(), pane_info, "/tmp/clive")
        assert result["type"] == "command_result"
        assert result["exit_code"] == 0
        pane_info.pane.send_keys.assert_called_once()

    def test_unknown_tool(self):
        from toolcall_runner import _execute_tool_call
        tc = {"name": "unknown_tool", "args": {}, "id": "c4"}
        result = _execute_tool_call(tc, _make_subtask(), _make_pane_info(), "/tmp/clive")
        assert result["type"] == "error"
        assert "Unknown tool" in result["message"]

    def test_run_command_blocked(self):
        from toolcall_runner import _execute_tool_call
        tc = {"name": "run_command", "args": {"command": "rm -rf /"}, "id": "c5"}
        result = _execute_tool_call(tc, _make_subtask(), _make_pane_info(), "/tmp/clive")
        assert result["type"] == "error"
        assert "BLOCKED" in result["message"]


# ---------------------------------------------------------------------------
# Test 7: Format detection (Anthropic vs OpenAI)
# ---------------------------------------------------------------------------

class TestFormatDetection:
    @patch("toolcall_runner.capture_pane")
    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.get_client")
    def test_anthropic_format(self, mock_client, mock_chat, mock_capture):
        """When client is Anthropic, parse_tool_calls should use 'anthropic' format."""
        import anthropic
        mock_anthropic_client = MagicMock(spec=anthropic.Anthropic)
        mock_client.return_value = mock_anthropic_client

        # Return a complete tool call — use Anthropic-style block
        tool_block = types.SimpleNamespace(
            type="tool_use", id="tu_1", name="complete",
            input={"summary": "done via anthropic"},
        )
        mock_chat.return_value = ([tool_block], "", 10, 5)
        mock_capture.return_value = "$ ready"

        subtask = _make_subtask()
        pane_info = _make_pane_info()

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(subtask, pane_info, dep_context="", session_dir="/tmp/clive")

        assert result.status == SubtaskStatus.COMPLETED
        assert result.summary == "done via anthropic"
