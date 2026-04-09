"""Tests for local-first address resolution via instance registry."""
import json
import os
import time
import tempfile

from agents import resolve_agent
from registry import register

REGISTRY_KWARGS = dict(
    pid=os.getpid(),
    tmux_session="clive-abc123",
    tmux_socket="clive",
    toolset="standard+media",
    task="monitoring logs",
    conversational=True,
    session_dir="/tmp/clive/abc123",
)


def test_local_resolution_finds_registry_entry(tmp_path):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    pane_def = resolve_agent("mybot", instance_registry_dir=tmp_path)
    assert pane_def["name"] == "agent-mybot"
    assert pane_def["app_type"] == "agent"
    assert "tmux" in pane_def["cmd"]
    assert "clive-abc123:conversational" in pane_def["cmd"]
    assert pane_def["host"] is None


def test_local_resolution_falls_through_when_not_found(tmp_path):
    # No local registry entry — should fall through to SSH-based resolution
    pane_def = resolve_agent("mybot", instance_registry_dir=tmp_path)
    assert pane_def["host"] == "mybot"
    assert "ssh" in pane_def["cmd"]


def test_local_resolution_skips_non_conversational(tmp_path):
    kwargs = {**REGISTRY_KWARGS, "conversational": False}
    register("mybot", **kwargs, registry_dir=tmp_path)
    pane_def = resolve_agent("mybot", instance_registry_dir=tmp_path)
    # Should fall through to SSH since not conversational
    assert pane_def["host"] == "mybot"
    assert "ssh" in pane_def["cmd"]


def test_local_pane_has_no_host(tmp_path):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    pane_def = resolve_agent("mybot", instance_registry_dir=tmp_path)
    assert pane_def["host"] is None


def test_local_shadows_remote(tmp_path):
    # Register local instance
    register("devbox", **REGISTRY_KWARGS, registry_dir=tmp_path)
    # Also provide a remote registry with "devbox"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("devbox:\n  host: devbox.remote.com\n  toolset: web\n")
        f.flush()
        try:
            pane_def = resolve_agent("devbox", registry_path=f.name,
                                     instance_registry_dir=tmp_path)
            # Local should win — no SSH, tmux attach instead
            assert pane_def["host"] is None
            assert "tmux" in pane_def["cmd"]
        finally:
            os.unlink(f.name)
