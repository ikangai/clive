"""Tests for named-instance session persistence (gh#13).

The live instance registry (registry.py) hard-deletes an entry the moment
its pid is dead, so a crashed/rebooted `--name foo` instance loses all
state. The persistence store keeps a restorable session snapshot that
survives process death, and cross-references the registry's liveness to
report which instances can be restored.
"""
import json
import os
import pytest


def _snap_kwargs(**over):
    base = dict(
        name="foo",
        toolset="standard",
        session_dir="/tmp/clive/foo",
        tmux_session="clive-foo",
        tmux_socket="clive",
        task="watch the build",
        conversational=True,
        panes=[{"name": "shell", "app_type": "shell"}],
    )
    base.update(over)
    return base


# ─── save / load round-trip ─────────────────────────────────────────────

def test_save_then_load_round_trips_spec(tmp_path):
    import persistence
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs())
    snap = persistence.load_snapshot("foo", persist_dir=tmp_path)
    assert snap is not None
    assert snap["name"] == "foo"
    assert snap["toolset"] == "standard"
    assert snap["session_dir"] == "/tmp/clive/foo"
    assert snap["panes"] == [{"name": "shell", "app_type": "shell"}]


def test_save_writes_named_json_file(tmp_path):
    import persistence
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs())
    assert (tmp_path / "foo.json").exists()


def test_save_stamps_saved_at_and_schema(tmp_path):
    import persistence
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs())
    snap = persistence.load_snapshot("foo", persist_dir=tmp_path)
    assert isinstance(snap.get("saved_at"), (int, float))
    assert "schema" in snap  # versioned so future format changes are detectable


def test_load_missing_returns_none(tmp_path):
    import persistence
    assert persistence.load_snapshot("ghost", persist_dir=tmp_path) is None


def test_save_overwrites_prior_snapshot(tmp_path):
    import persistence
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs(task="old"))
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs(task="new"))
    snap = persistence.load_snapshot("foo", persist_dir=tmp_path)
    assert snap["task"] == "new"


def test_load_corrupt_snapshot_returns_none_and_prunes(tmp_path):
    import persistence
    (tmp_path / "bar.json").write_text("{ not valid json")
    assert persistence.load_snapshot("bar", persist_dir=tmp_path) is None
    assert not (tmp_path / "bar.json").exists()  # corrupt file pruned


# ─── persistence survives "process death" (unlike the live registry) ────

def test_snapshot_persists_independent_of_registry(tmp_path):
    """The whole point: a snapshot is NOT pruned when the pid dies."""
    import persistence
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs())
    # Simulate the process having died — snapshot must still be loadable.
    snap = persistence.load_snapshot("foo", persist_dir=tmp_path)
    assert snap is not None


# ─── list / clear ───────────────────────────────────────────────────────

def test_list_snapshots_returns_all(tmp_path):
    import persistence
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs(name="a"))
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs(name="b"))
    names = {s["name"] for s in persistence.list_snapshots(persist_dir=tmp_path)}
    assert names == {"a", "b"}


def test_list_snapshots_empty_dir(tmp_path):
    import persistence
    assert persistence.list_snapshots(persist_dir=tmp_path / "nope") == []


def test_clear_snapshot_removes_file(tmp_path):
    import persistence
    persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs())
    assert persistence.clear_snapshot("foo", persist_dir=tmp_path) is True
    assert persistence.load_snapshot("foo", persist_dir=tmp_path) is None


def test_clear_missing_snapshot_returns_false(tmp_path):
    import persistence
    assert persistence.clear_snapshot("ghost", persist_dir=tmp_path) is False


# ─── restorable_instances: cross-reference registry liveness ────────────

def test_restorable_lists_dead_instances_only(tmp_path, monkeypatch):
    import persistence
    persist_dir = tmp_path / "persist"
    persistence.save_snapshot(persist_dir=persist_dir, **_snap_kwargs(name="alive"))
    persistence.save_snapshot(persist_dir=persist_dir, **_snap_kwargs(name="dead"))

    # 'alive' has a live registry entry; 'dead' does not.
    import registry
    monkeypatch.setattr(
        registry, "get_instance",
        lambda name, registry_dir=None: {"pid": 123} if name == "alive" else None,
    )

    restorable = persistence.restorable_instances(
        persist_dir=persist_dir, registry_dir=tmp_path / "reg"
    )
    names = {s["name"] for s in restorable}
    assert names == {"dead"}  # only the non-live instance is restorable


def test_restorable_empty_when_all_live(tmp_path, monkeypatch):
    import persistence, registry
    persist_dir = tmp_path / "persist"
    persistence.save_snapshot(persist_dir=persist_dir, **_snap_kwargs(name="x"))
    monkeypatch.setattr(registry, "get_instance",
                        lambda name, registry_dir=None: {"pid": 1})
    assert persistence.restorable_instances(persist_dir=persist_dir) == []


# ─── name validation parity with registry (defense in depth) ────────────

def test_save_rejects_unsafe_name(tmp_path):
    import persistence
    with pytest.raises(ValueError):
        persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs(name="../etc/passwd"))


def test_save_rejects_name_with_slash(tmp_path):
    import persistence
    with pytest.raises(ValueError):
        persistence.save_snapshot(persist_dir=tmp_path, **_snap_kwargs(name="a/b"))
