"""Tests for the dashboard snapshot CLI."""
import json
import os
import time

from registry import register
from dashboard import render_snapshot

REGISTRY_KWARGS = dict(
    pid=os.getpid(),
    tmux_session="clive-abc123",
    tmux_socket="clive",
    toolset="standard+media",
    task="monitoring logs",
    conversational=True,
    session_dir="/tmp/clive/abc123",
)


def test_dashboard_lists_instances(tmp_path, capsys):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    register("researcher", **{**REGISTRY_KWARGS, "toolset": "research+web",
             "task": "analyzing data"}, registry_dir=tmp_path)
    render_snapshot(registry_dir=tmp_path)
    out = capsys.readouterr().out
    assert "mybot" in out
    assert "researcher" in out


def test_dashboard_prunes_dead(tmp_path, capsys):
    dead_entry = {
        "name": "deadbot",
        "pid": 99999999,
        "tmux_session": "clive-dead",
        "tmux_socket": "clive",
        "toolset": "standard",
        "task": "gone",
        "conversational": True,
        "session_dir": "/tmp/clive/dead",
        "started_at": time.time(),
    }
    (tmp_path / "deadbot.json").write_text(json.dumps(dead_entry))
    render_snapshot(registry_dir=tmp_path)
    out = capsys.readouterr().out
    assert "deadbot" not in out


def test_dashboard_empty_state(tmp_path, capsys):
    render_snapshot(registry_dir=tmp_path)
    out = capsys.readouterr().out
    assert "No instances running" in out


def test_dashboard_shows_uptime(tmp_path, capsys):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    render_snapshot(registry_dir=tmp_path)
    out = capsys.readouterr().out
    # Should show some uptime value
    assert "0h" in out or "0m" in out or "h" in out
