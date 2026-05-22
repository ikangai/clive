"""Tests for discovery.explorer — adapter over run_subtask_interactive.

Real-pane integration is exercised by the manual smoke test in task 7's
Verification section; unit tests here mock the runner and pane lifecycle.
"""
from unittest.mock import MagicMock

import pytest

from discovery.explorer import _check_exploration_safety, explore_tool
from discovery.models import ExplorationResult, ProbeOutcome


# ─── Exploration safety unit tests (no LLM, no pane) ─────────────────────


def test_safety_allows_basic_help():
    assert _check_exploration_safety("rg --help", "rg") is None
    assert _check_exploration_safety("rg -h", "rg") is None
    assert _check_exploration_safety("man rg 2>&1 | head -80", "rg") is None


def test_safety_blocks_credential_tool_without_help_flag():
    v = _check_exploration_safety("aws s3 ls", "aws")
    assert v is not None
    assert "credential" in v.lower() or "help" in v.lower()


def test_safety_allows_credential_tool_with_help_flag():
    assert _check_exploration_safety("aws --help", "aws") is None
    assert _check_exploration_safety("aws --version", "aws") is None
    assert _check_exploration_safety("kubectl -h", "kubectl") is None


def test_safety_blocks_tui_tool_without_help():
    v = _check_exploration_safety("vim file.txt", "vim")
    assert v is not None
    assert "interactive" in v.lower() or "tui" in v.lower() or "help" in v.lower()


def test_safety_allows_tui_tool_with_help():
    assert _check_exploration_safety("vim --help", "vim") is None


def test_safety_still_blocks_destructive_from_underlying_check():
    v = _check_exploration_safety("rm -rf /", "rg")
    assert v is not None
    assert "Blocked" in v


def test_safety_strips_sudo_prefix():
    # `sudo aws s3 ls` should be flagged the same as `aws s3 ls`.
    v = _check_exploration_safety("sudo aws s3 ls", "aws")
    assert v is not None


# ─── explore_tool integration tests (everything mocked) ──────────────────


class _FakeRun:
    """Stand-in for run_subtask_interactive driving the on_event callback
    with a scripted sequence of (cmd, screen, exit_code) tuples."""

    def __init__(self, script, summary="explored"):
        self.script = script
        self.summary = summary

    def __call__(self, subtask, pane_info, dep_context, on_event=None, session_dir="/tmp/clive"):
        from models import SubtaskResult, SubtaskStatus
        for i, (cmd, screen, exit_code) in enumerate(self.script, start=1):
            if on_event:
                on_event("probe", subtask.id, cmd, exit_code, screen)
        return SubtaskResult(
            subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
            summary=self.summary, output_snippet="", turns_used=len(self.script),
            prompt_tokens=10, completion_tokens=10,
        )


def test_explore_tool_records_probes(monkeypatch, tmp_path):
    script = [
        ("echo --help", "Usage: echo [OPTION]...", 0),
        ("echo --version", "echo 8.32", 0),
    ]
    monkeypatch.setattr("discovery.explorer.run_subtask_interactive", _FakeRun(script))
    monkeypatch.setattr("discovery.explorer._open_exploration_pane", lambda sd: MagicMock(name="pane"))
    monkeypatch.setattr("discovery.explorer._close_exploration_pane", lambda p: None)

    result = explore_tool("echo", session_dir_root=str(tmp_path))

    assert isinstance(result, ExplorationResult)
    assert result.tool_name == "echo"
    assert len(result.probes) == 2
    assert result.probes[0].command == "echo --help"
    assert result.probes[0].exit_code == 0
    assert "Usage" in result.probes[0].screen
    assert result.summary == "explored"


def test_explore_tool_empty_when_no_probes(monkeypatch, tmp_path):
    monkeypatch.setattr("discovery.explorer.run_subtask_interactive", _FakeRun([], summary=""))
    monkeypatch.setattr("discovery.explorer._open_exploration_pane", lambda sd: MagicMock())
    monkeypatch.setattr("discovery.explorer._close_exploration_pane", lambda p: None)

    result = explore_tool("nothing", session_dir_root=str(tmp_path))
    assert result.tool_name == "nothing"
    assert result.probes == []


def test_explore_tool_uses_unique_session_dir_per_call(monkeypatch, tmp_path):
    captured = []

    def fake_run(subtask, pane_info, dep_context, on_event=None, session_dir="/tmp/clive"):
        from models import SubtaskResult, SubtaskStatus
        captured.append(session_dir)
        return SubtaskResult(
            subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
            summary="", output_snippet="", turns_used=0,
            prompt_tokens=0, completion_tokens=0,
        )

    monkeypatch.setattr("discovery.explorer.run_subtask_interactive", fake_run)
    monkeypatch.setattr("discovery.explorer._open_exploration_pane", lambda sd: MagicMock())
    monkeypatch.setattr("discovery.explorer._close_exploration_pane", lambda p: None)

    explore_tool("rg", session_dir_root=str(tmp_path))
    explore_tool("rg", session_dir_root=str(tmp_path))
    # Two calls — two distinct session_dirs even with the same tool name.
    assert len(captured) == 2
    assert captured[0] != captured[1]
    assert all("rg" in sd for sd in captured)
    assert all(sd.startswith(str(tmp_path)) for sd in captured)


def test_explore_tool_closes_pane_even_on_runner_exception(monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(
        "discovery.explorer.run_subtask_interactive",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr("discovery.explorer._open_exploration_pane", lambda sd: "PANE")
    monkeypatch.setattr("discovery.explorer._close_exploration_pane", lambda p: closed.append(p))

    with pytest.raises(RuntimeError):
        explore_tool("rg", session_dir_root=str(tmp_path))
    assert closed == ["PANE"]
