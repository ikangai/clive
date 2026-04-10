"""Tests for the slash-command registry (commands.py) and its TUI integration.

The registry is the single source of truth for slash commands. These tests
lock in the invariants that make it so:

1. Every command registered by tui.py has a non-empty name and summary.
2. render_help() produces output mentioning every registered command.
3. dispatch() returns False for unknown commands (enables "Unknown command"
   fallback in tui.py::_handle_command).
4. Session slash commands are reachable via the registry (not orphaned).
5. The previously-parallel HELP_TEXT block is gone.
"""
import os
import pytest

import commands


@pytest.fixture(autouse=True)
def _reset_registry_after_tui_import():
    """Import tui so registrations run, then yield. The registry is module-
    level state, so these tests read it rather than mutating it."""
    import tui  # noqa: F401 — side effect: registers all core + session commands
    yield


def test_registry_non_empty():
    assert len(commands.all_commands()) >= 20, (
        "expected at least 20 registered commands (14 core + 6 session)"
    )


def test_every_command_has_name_and_summary():
    for c in commands.all_commands():
        assert c.name.startswith("/"), f"name missing leading slash: {c.name!r}"
        assert len(c.name) >= 2, f"name too short: {c.name!r}"
        assert c.summary, f"{c.name} has empty summary"
        assert callable(c.handler), f"{c.name} handler is not callable"
        assert c.source in {"core", "session"}, (
            f"{c.name} has unexpected source: {c.source!r}"
        )


def test_core_commands_all_present():
    """The 14 commands that used to live in the if/elif ladder."""
    expected = {
        "/help", "/profile", "/provider", "/model", "/tools", "/install",
        "/status", "/cancel", "/clear", "/selfmod", "/undo", "/safe-mode",
        "/evolve", "/dashboard",
    }
    names = set(commands.names())
    missing = expected - names
    assert not missing, f"core commands missing from registry: {sorted(missing)}"


def test_session_commands_all_present():
    """The 6 commands formerly orphaned in session_store.dispatch_session_slash."""
    expected = {"/sessions", "/new", "/resume", "/title", "/session", "/id"}
    names = set(commands.names())
    missing = expected - names
    assert not missing, f"session commands missing from registry: {sorted(missing)}"


def test_session_commands_tagged_as_session_source():
    session_names = {"/sessions", "/new", "/resume", "/title", "/session", "/id"}
    for name in session_names:
        c = commands.get(name)
        assert c is not None, f"{name} not registered"
        assert c.source == "session", (
            f"{name} has source={c.source!r}, expected 'session'"
        )


def test_render_help_mentions_every_command():
    help_text = commands.render_help(
        profiles="standard", categories="core", providers="anthropic"
    )
    for c in commands.all_commands():
        assert c.name in help_text, f"{c.name} missing from rendered help"
        assert c.summary in help_text, f"summary for {c.name} missing from rendered help"


def test_render_help_mentions_profile_provider_category_values():
    help_text = commands.render_help(
        profiles="ALPHA", categories="BETA", providers="GAMMA"
    )
    assert "ALPHA" in help_text
    assert "BETA" in help_text
    assert "GAMMA" in help_text


def test_dispatch_unknown_returns_false():
    class _FakeOut:
        def __init__(self):
            self.lines = []

        def write(self, s):
            self.lines.append(s)

    class _FakeApp:
        _active_sid = None

    app, out = _FakeApp(), _FakeOut()
    handled = commands.dispatch("/nonexistent", "", app, out)
    assert handled is False
    assert out.lines == [], "dispatch should not write to out for unknown commands"


def test_dispatch_known_returns_true():
    """A no-op command like /clear should be handled via registry."""
    class _FakeOut:
        def __init__(self):
            self.cleared = False
            self.lines = []

        def clear(self):
            self.cleared = True

        def write(self, s):
            self.lines.append(s)

    class _FakeApp:
        _active_sid = None

    app, out = _FakeApp(), _FakeOut()
    handled = commands.dispatch("/clear", "", app, out)
    assert handled is True
    assert out.cleared is True


def test_tui_theme_no_longer_defines_help_text():
    """HELP_TEXT was replaced by commands.render_help(). Ensure the static
    block hasn't sneaked back into tui_theme.py as a module-level binding."""
    import tui_theme
    assert not hasattr(tui_theme, "HELP_TEXT"), (
        "tui_theme.HELP_TEXT must not exist — render from commands.render_help()"
    )


def test_no_if_cmd_ladder_in_tui_or_session_store():
    """Regression guard: the branch-count metric must stay at 0."""
    import re
    root = os.path.dirname(os.path.dirname(__file__))
    pattern = re.compile(r"^\s+(if|elif)\s+cmd\s")
    for rel in ("tui.py", "session_store.py"):
        path = os.path.join(root, rel)
        with open(path) as f:
            matches = [
                (i + 1, line.rstrip())
                for i, line in enumerate(f)
                if pattern.match(line)
            ]
        assert matches == [], (
            f"hardcoded slash-command dispatch ladder found in {rel}: {matches}"
        )
