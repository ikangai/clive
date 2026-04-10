"""Tests for TUI /dashboard slash command."""
import os


def test_slash_dashboard_recognized():
    """Verify /dashboard is registered in the slash-command registry.

    Previously this test grepped tui.py source for the literal '"/dashboard"'.
    After the registry refactor, command registration lives in tui_commands.py
    and the authoritative check is "is /dashboard in the registry?" — which
    is strictly more precise than a string search.
    """
    import tui  # noqa: F401 — side effect: registers all commands
    import commands
    assert commands.get("/dashboard") is not None, (
        "/dashboard not registered in the slash-command registry"
    )


def test_dashboard_in_help_text():
    """Verify /dashboard appears in the rendered help output."""
    import tui  # noqa: F401
    import commands
    rendered = commands.render_help(profiles="", categories="", providers="")
    assert "/dashboard" in rendered, "/dashboard not in rendered help"


def test_render_lines_returns_list(tmp_path):
    """Dashboard render_lines() returns list of strings for TUI embedding."""
    from dashboard import render_lines
    lines = render_lines(registry_dir=tmp_path,
                         agents_yaml_path=str(tmp_path / "nonexistent.yaml"))
    assert isinstance(lines, list)
    assert any("No instances" in line for line in lines)
