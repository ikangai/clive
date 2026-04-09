"""Tests for the dashboard snapshot CLI."""
import json
import os
import tempfile
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
    render_snapshot(registry_dir=tmp_path,
                    agents_yaml_path=str(tmp_path / "nonexistent.yaml"))
    out = capsys.readouterr().out
    assert "No instances running" in out


def test_dashboard_shows_uptime(tmp_path, capsys):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    render_snapshot(registry_dir=tmp_path)
    out = capsys.readouterr().out
    assert "0h" in out or "0m" in out or "h" in out


def test_dashboard_shows_status_column(tmp_path, capsys):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    render_snapshot(registry_dir=tmp_path)
    out = capsys.readouterr().out
    assert "STATUS" in out
    assert "idle" in out


def test_dashboard_shows_remote_from_agents_yaml(tmp_path, capsys):
    # Create a local instance so dashboard isn't completely empty
    register("local1", **REGISTRY_KWARGS, registry_dir=tmp_path)
    # Create a temp agents.yaml with a remote entry
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("devbox:\n  host: devbox.remote.com\n  toolset: web\n")
        f.flush()
        try:
            render_snapshot(registry_dir=tmp_path, agents_yaml_path=f.name)
            out = capsys.readouterr().out
            assert "REMOTE AGENTS" in out
            assert "devbox" in out
            assert "devbox.remote.com" in out
        finally:
            os.unlink(f.name)


def test_dashboard_summary_footer(tmp_path, capsys):
    register("bot1", **REGISTRY_KWARGS, registry_dir=tmp_path)
    register("bot2", **REGISTRY_KWARGS, registry_dir=tmp_path)
    render_snapshot(registry_dir=tmp_path)
    out = capsys.readouterr().out
    assert "2 instances" in out
