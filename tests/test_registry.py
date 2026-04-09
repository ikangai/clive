"""Tests for the file-based instance registry."""
import json
import os
import time

from registry import register, deregister, list_instances, get_instance, is_name_available

REGISTRY_KWARGS = dict(
    pid=os.getpid(),
    tmux_session="clive-test1234",
    tmux_socket="clive",
    toolset="standard",
    task="test task",
    conversational=True,
    session_dir="/tmp/clive/test1234",
)


def test_register_creates_file(tmp_path):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    f = tmp_path / "mybot.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data["name"] == "mybot"
    assert data["pid"] == os.getpid()
    assert data["tmux_session"] == "clive-test1234"
    assert data["conversational"] is True
    assert "started_at" in data


def test_deregister_removes_file(tmp_path):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    assert (tmp_path / "mybot.json").exists()
    result = deregister("mybot", registry_dir=tmp_path)
    assert result is True
    assert not (tmp_path / "mybot.json").exists()


def test_deregister_returns_false_for_missing(tmp_path):
    result = deregister("nonexistent", registry_dir=tmp_path)
    assert result is False


def test_list_instances(tmp_path):
    register("bot1", **REGISTRY_KWARGS, registry_dir=tmp_path)
    register("bot2", **REGISTRY_KWARGS, registry_dir=tmp_path)
    instances = list_instances(registry_dir=tmp_path)
    names = [i["name"] for i in instances]
    assert "bot1" in names
    assert "bot2" in names


def test_list_prunes_dead_pids(tmp_path):
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
    instances = list_instances(registry_dir=tmp_path)
    assert len(instances) == 0
    assert not (tmp_path / "deadbot.json").exists()


def test_get_instance(tmp_path):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    inst = get_instance("mybot", registry_dir=tmp_path)
    assert inst is not None
    assert inst["name"] == "mybot"


def test_get_instance_returns_none_for_missing(tmp_path):
    assert get_instance("nope", registry_dir=tmp_path) is None


def test_get_instance_returns_none_for_dead_pid(tmp_path):
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
    assert get_instance("deadbot", registry_dir=tmp_path) is None


def test_name_collision_detected(tmp_path):
    register("mybot", **REGISTRY_KWARGS, registry_dir=tmp_path)
    assert not is_name_available("mybot", registry_dir=tmp_path)


def test_name_available_after_pid_dies(tmp_path):
    dead_entry = {
        "name": "mybot",
        "pid": 99999999,
        "tmux_session": "clive-dead",
        "tmux_socket": "clive",
        "toolset": "standard",
        "task": "gone",
        "conversational": True,
        "session_dir": "/tmp/clive/dead",
        "started_at": time.time(),
    }
    (tmp_path / "mybot.json").write_text(json.dumps(dead_entry))
    assert is_name_available("mybot", registry_dir=tmp_path)


def test_name_available_when_no_entry(tmp_path):
    assert is_name_available("newbot", registry_dir=tmp_path)
