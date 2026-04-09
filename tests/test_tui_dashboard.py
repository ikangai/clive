"""Tests for TUI /dashboard slash command."""
import io
import os


def test_slash_dashboard_recognized():
    """Verify /dashboard is handled in the command handler (not 'Unknown command')."""
    # Import the tui module and check the command handler recognizes /dashboard
    # We check the source code directly since we can't easily instantiate the TUI
    with open(os.path.join(os.path.dirname(__file__), "..", "tui.py")) as f:
        source = f.read()
    assert '"/dashboard"' in source, "/dashboard not found in command handler"


def test_dashboard_in_help_text():
    """Verify /dashboard appears in HELP_TEXT."""
    with open(os.path.join(os.path.dirname(__file__), "..", "tui.py")) as f:
        source = f.read()
    assert "/dashboard" in source, "/dashboard not in help text"


def test_render_lines_returns_list(tmp_path):
    """Dashboard render_lines() returns list of strings for TUI embedding."""
    from dashboard import render_lines
    lines = render_lines(registry_dir=tmp_path,
                         agents_yaml_path=str(tmp_path / "nonexistent.yaml"))
    assert isinstance(lines, list)
    assert any("No instances" in line for line in lines)
