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


def test_complete_command_name_all():
    """Empty prefix returns every registered command."""
    result = commands.complete_command_name("")
    assert len(result) == len(commands.all_commands())


def test_complete_command_name_prefix():
    """'/pr' should match /profile and /provider."""
    result = commands.complete_command_name("/pr")
    assert "/profile" in result
    assert "/provider" in result
    assert "/help" not in result


def test_complete_command_name_case_insensitive():
    assert "/PROFILE" not in commands.names(), "sanity: names stay lowercase"
    assert "/profile" in commands.complete_command_name("/PR")


def test_complete_arg_profile_returns_known_profiles():
    """/profile completer should include the built-in profile names."""
    from toolsets import PROFILES
    result = commands.complete_arg("/profile", "")
    # Every hardcoded profile should appear
    for p in PROFILES:
        assert p in result, f"profile {p} missing from /profile completions"


def test_complete_arg_provider_returns_known_providers():
    from llm import PROVIDERS
    result = commands.complete_arg("/provider", "")
    for name in PROVIDERS:
        assert name in result, f"provider {name} missing from /provider completions"


def test_complete_arg_evolve_fixed_choices():
    result = commands.complete_arg("/evolve", "")
    assert set(result) == {"shell", "browser", "all"}


def test_complete_arg_evolve_prefix_filter():
    assert commands.complete_arg("/evolve", "sh") == ["shell"]
    assert commands.complete_arg("/evolve", "b") == ["browser"]


def test_complete_arg_unknown_command_returns_empty():
    assert commands.complete_arg("/nope", "") == []


def test_complete_arg_no_completer_returns_empty():
    """Commands without a registered completer return []."""
    assert commands.complete_arg("/help", "") == []
    assert commands.complete_arg("/status", "") == []


def test_format_command_list_groups_by_source():
    lines = commands.format_command_list()
    text = "\n".join(lines)
    # Section headers
    assert "core" in text
    assert "session" in text
    # Every command listed
    for c in commands.all_commands():
        assert c.name in text, f"{c.name} missing from inline listing"


def test_format_command_list_core_before_session():
    lines = commands.format_command_list()
    text = "\n".join(lines)
    assert text.index("core") < text.index("session"), (
        "core commands should come before session commands in the listing"
    )


def test_suggest_typo_close_match():
    """Common typos should return the intended command."""
    assert "/profile" in commands.suggest("/profil")
    assert "/provider" in commands.suggest("/provide")


def test_suggest_unknown_command_returns_empty_for_gibberish():
    assert commands.suggest("/xyzabc") == []


def test_suggest_respects_limit():
    result = commands.suggest("/s", limit=2)
    assert len(result) <= 2


def test_build_slash_hint_exact_match_no_arg():
    """Exact /help match without typing arg → show summary."""
    hint = commands.build_slash_hint("/help", "", typing_arg=False)
    assert "/help" in hint
    assert "Show this help" in hint


def test_build_slash_hint_exact_match_with_arg_completions():
    """Exact /evolve + typing_arg → show argument completions."""
    hint = commands.build_slash_hint("/evolve", "", typing_arg=True)
    assert "shell" in hint
    assert "browser" in hint
    assert "all" in hint


def test_build_slash_hint_exact_match_arg_prefix_filters():
    hint = commands.build_slash_hint("/evolve", "sh", typing_arg=True)
    assert "shell" in hint
    assert "browser" not in hint


def test_build_slash_hint_partial_name_match():
    """/pr → matches /profile and /provider."""
    hint = commands.build_slash_hint("/pr", "", typing_arg=False)
    assert "matches:" in hint
    assert "/profile" in hint
    assert "/provider" in hint


def test_build_slash_hint_empty_for_gibberish():
    hint = commands.build_slash_hint("/zzxq", "", typing_arg=False)
    assert hint == ""


def test_load_plugin_commands_registers_new_command(tmp_path):
    """A drop-in plugin file can register a command via the normal API."""
    plugin = tmp_path / "history.py"
    plugin.write_text(
        "import commands\n"
        "def _handler(app, arg, out):\n"
        "    out.write('history listing')\n"
        "commands.register(commands.SlashCommand(\n"
        "    name='/history_test_tmp', summary='Test plugin command',\n"
        "    args_hint='', handler=_handler, source='plugin'))\n"
    )
    loaded = commands.load_plugin_commands(str(tmp_path))
    try:
        assert "history.py" in loaded
        cmd = commands.get("/history_test_tmp")
        assert cmd is not None
        assert cmd.source == "plugin"
        assert cmd.summary == "Test plugin command"
    finally:
        # Clean up — don't leak into other tests
        commands._REGISTRY.pop("/history_test_tmp", None)


def test_load_plugin_commands_missing_dir_returns_empty(tmp_path):
    nonexistent = tmp_path / "nowhere"
    assert commands.load_plugin_commands(str(nonexistent)) == []


def test_load_plugin_commands_skips_underscore_prefixed(tmp_path):
    (tmp_path / "_private.py").write_text(
        "import commands\n"
        "commands.register(commands.SlashCommand(\n"
        "    name='/should_not_load', summary='x', handler=lambda a,b,c: None))\n"
    )
    commands.load_plugin_commands(str(tmp_path))
    assert commands.get("/should_not_load") is None


def test_load_plugin_commands_broken_plugin_does_not_crash(tmp_path):
    """Broken plugins must be silently skipped — a plugin error never blocks the TUI."""
    (tmp_path / "good.py").write_text(
        "import commands\n"
        "commands.register(commands.SlashCommand(\n"
        "    name='/good_plugin_tmp', summary='ok', handler=lambda a,b,c: None, source='plugin'))\n"
    )
    (tmp_path / "bad.py").write_text("this is not valid python syntax !!!!")
    loaded = commands.load_plugin_commands(str(tmp_path))
    try:
        assert "good.py" in loaded
        assert "bad.py" not in loaded
        assert commands.get("/good_plugin_tmp") is not None
    finally:
        commands._REGISTRY.pop("/good_plugin_tmp", None)


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
