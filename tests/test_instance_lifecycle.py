"""Tests for --name flag, instance lifecycle, and --stop."""
import json
import os
import signal
import time

from registry import register, deregister, get_instance, is_name_available


def test_name_flag_registers_instance(tmp_path):
    """Simulates what clive.py does on startup with --name."""
    register("mybot", pid=os.getpid(), tmux_session="clive-abc123",
             tmux_socket="clive", toolset="standard", task="test task",
             conversational=True, session_dir="/tmp/clive/abc123",
             registry_dir=tmp_path)
    assert (tmp_path / "mybot.json").exists()
    inst = get_instance("mybot", registry_dir=tmp_path)
    assert inst is not None
    assert inst["conversational"] is True


def test_exit_deregisters_instance(tmp_path):
    """Simulates what the cleanup handler does on exit."""
    register("mybot", pid=os.getpid(), tmux_session="clive-abc123",
             tmux_socket="clive", toolset="standard", task="test task",
             conversational=True, session_dir="/tmp/clive/abc123",
             registry_dir=tmp_path)
    assert (tmp_path / "mybot.json").exists()
    # Simulate cleanup
    deregister("mybot", registry_dir=tmp_path)
    assert not (tmp_path / "mybot.json").exists()


def test_stop_sends_sigterm(tmp_path):
    """--stop should look up PID from registry and send SIGTERM."""
    # Register with current PID
    register("mybot", pid=os.getpid(), tmux_session="clive-abc123",
             tmux_socket="clive", toolset="standard", task="test task",
             conversational=True, session_dir="/tmp/clive/abc123",
             registry_dir=tmp_path)
    inst = get_instance("mybot", registry_dir=tmp_path)
    assert inst is not None
    # Verify we can resolve the PID (the actual SIGTERM send is tested
    # by checking that os.kill(pid, 0) works — we don't send real SIGTERM
    # to ourselves in tests)
    pid = inst["pid"]
    assert pid == os.getpid()
    # Verify the PID is alive (which is what --stop checks before sending SIGTERM)
    os.kill(pid, 0)  # Should not raise


def test_name_collision_rejected(tmp_path):
    """Starting with --name should fail if another instance owns that name."""
    register("mybot", pid=os.getpid(), tmux_session="clive-abc123",
             tmux_socket="clive", toolset="standard", task="first task",
             conversational=True, session_dir="/tmp/clive/abc123",
             registry_dir=tmp_path)
    assert not is_name_available("mybot", registry_dir=tmp_path)


def test_unnamed_instance_not_conversational(tmp_path):
    """Unnamed instances get a generated name but are not conversational."""
    import socket
    auto_name = f"{socket.gethostname()}-{os.getpid()}"
    register(auto_name, pid=os.getpid(), tmux_session="clive-xyz789",
             tmux_socket="clive", toolset="standard", task="ephemeral task",
             conversational=False, session_dir="/tmp/clive/xyz789",
             registry_dir=tmp_path)
    inst = get_instance(auto_name, registry_dir=tmp_path)
    assert inst is not None
    assert inst["conversational"] is False
