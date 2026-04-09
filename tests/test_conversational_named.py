"""Tests for conversational loop behavior with named instances.

These test the protocol logic and loop structure rather than spawning
full clive processes, since the conversational loop relies on stdin/stdout
and the full tmux session infrastructure.
"""
import io
import json
import os
import time

from registry import register, get_instance, deregister


def test_named_instance_is_conversational(tmp_path):
    """Named instances are registered as conversational."""
    register("mybot", pid=os.getpid(), tmux_session="clive-abc123",
             tmux_socket="clive", toolset="standard", task="initial",
             conversational=True, session_dir="/tmp/clive/abc123",
             registry_dir=tmp_path)
    inst = get_instance("mybot", registry_dir=tmp_path)
    assert inst["conversational"] is True


def test_named_instance_survives_deregister_reregister(tmp_path):
    """A named instance can deregister and re-register (simulating restart)."""
    register("mybot", pid=os.getpid(), tmux_session="clive-abc123",
             tmux_socket="clive", toolset="standard", task="task1",
             conversational=True, session_dir="/tmp/clive/abc123",
             registry_dir=tmp_path)
    deregister("mybot", registry_dir=tmp_path)
    assert get_instance("mybot", registry_dir=tmp_path) is None
    # Re-register
    register("mybot", pid=os.getpid(), tmux_session="clive-def456",
             tmux_socket="clive", toolset="standard", task="task2",
             conversational=True, session_dir="/tmp/clive/def456",
             registry_dir=tmp_path)
    inst = get_instance("mybot", registry_dir=tmp_path)
    assert inst["task"] == "task2"


def test_turn_protocol_markers():
    """Verify TURN:/CONTEXT:/DONE: protocol markers are importable."""
    from output import emit_turn, emit_context
    # These functions should exist and be callable
    assert callable(emit_turn)
    assert callable(emit_context)


def test_stop_command_recognized():
    """The /stop command should break the conversational loop.

    We verify this by checking the source code contains the exit conditions.
    """
    with open(os.path.join(os.path.dirname(__file__), "..", "clive.py")) as f:
        source = f.read()
    assert '"/stop"' in source, "/stop not recognized as exit command"
    assert "keep_alive" in source, "keep_alive flag not found in conversational mode"
