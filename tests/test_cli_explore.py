"""Tests for the --explore CLI flag (gh#41)."""
from unittest.mock import MagicMock

import pytest

from cli_args import build_parser
from discovery.models import ExplorationResult


def test_parser_accepts_explore_flag():
    parser = build_parser()
    args = parser.parse_args(["--explore", "rg"])
    assert args.explore == "rg"
    assert args.explore_overwrite is False


def test_parser_accepts_explore_overwrite():
    parser = build_parser()
    args = parser.parse_args(["--explore", "rg", "--explore-overwrite"])
    assert args.explore_overwrite is True


def test_parser_explore_optional():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.explore is None


def test_handle_explore_runs_pipeline(monkeypatch, capsys):
    import cli_handlers

    fake_result = ExplorationResult(tool_name="rg", summary="ripgrep")
    monkeypatch.setattr(cli_handlers, "explore_tool", lambda name, **kw: fake_result)
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\nx\n---\n")
    written = []
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        lambda name, text, overwrite=False: (written.append((name, text, overwrite)) or "/p/rg.md"),
    )

    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)

    assert rc == 0
    assert written == [("rg", "---\nx\n---\n", False)]
    out = capsys.readouterr().out
    assert "/p/rg.md" in out
    assert "ripgrep" in out


def test_handle_explore_passes_overwrite_flag(monkeypatch, capsys):
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "explore_tool",
        lambda name, **kw: ExplorationResult(tool_name=name, summary="s"),
    )
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\n---\n")
    written = []
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        lambda name, text, overwrite=False: (written.append(overwrite) or "/p/rg.md"),
    )
    args = MagicMock(explore="rg", explore_overwrite=True)
    rc = cli_handlers.handle_explore(args)
    assert rc == 0
    assert written == [True]


def test_handle_explore_returns_nonzero_on_existing_driver(monkeypatch, capsys):
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "explore_tool",
        lambda name, **kw: ExplorationResult(tool_name=name, summary="s"),
    )
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\n---\n")
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        MagicMock(side_effect=FileExistsError("exists")),
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0
    out = capsys.readouterr().out
    assert "exists" in out
    assert "--explore-overwrite" in out


def test_handle_explore_returns_nonzero_on_malformed_llm(monkeypatch, capsys):
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "explore_tool",
        lambda name, **kw: ExplorationResult(tool_name=name, summary=""),
    )
    monkeypatch.setattr(
        cli_handlers, "generate_driver",
        MagicMock(side_effect=ValueError("missing section")),
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0


def test_handle_explore_returns_nonzero_on_explore_failure(monkeypatch, capsys):
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "explore_tool",
        MagicMock(side_effect=RuntimeError("tmux unavailable")),
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0
    out = capsys.readouterr().out
    assert "tmux unavailable" in out


def test_handle_explore_warns_when_no_summary(monkeypatch, capsys):
    import cli_handlers

    fake = ExplorationResult(tool_name="rg", summary="")
    fake.probes = [MagicMock(), MagicMock(), MagicMock()]
    monkeypatch.setattr(cli_handlers, "explore_tool", lambda name, **kw: fake)
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\n---\n")
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        lambda name, text, overwrite=False: "/p/rg.md",
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Warning" in out or "without DONE" in out
    assert "3 probes" in out  # informative even on no-summary path
