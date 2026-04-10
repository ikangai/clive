"""Tests for the persistent chat-session store."""
import json

from session_store import new, get, list_sessions, delete, append_message


def test_new_creates_file(tmp_path):
    sid = new(title="first chat", sessions_dir=tmp_path)
    f = tmp_path / f"{sid}.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data["id"] == sid
    assert data["title"] == "first chat"
    assert "created_at" in data
    assert "updated_at" in data


def test_new_returns_unique_ids(tmp_path):
    a = new(sessions_dir=tmp_path)
    b = new(sessions_dir=tmp_path)
    assert a != b


def test_new_default_title_is_empty_string(tmp_path):
    sid = new(sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert data is not None
    assert data["title"] == ""


def test_get_returns_none_for_missing(tmp_path):
    assert get("nonexistent", sessions_dir=tmp_path) is None


def test_get_returns_session_data(tmp_path):
    sid = new(title="hello", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert data is not None
    assert data["id"] == sid
    assert data["title"] == "hello"


def test_list_sessions_empty(tmp_path):
    assert list_sessions(sessions_dir=tmp_path) == []


def test_list_sessions_returns_all(tmp_path):
    a = new(title="one", sessions_dir=tmp_path)
    b = new(title="two", sessions_dir=tmp_path)
    sessions = list_sessions(sessions_dir=tmp_path)
    ids = {s["id"] for s in sessions}
    assert a in ids
    assert b in ids
    assert len(sessions) == 2


def test_list_sessions_skips_malformed(tmp_path):
    new(sessions_dir=tmp_path)
    (tmp_path / "garbage.json").write_text("{not valid json")
    sessions = list_sessions(sessions_dir=tmp_path)
    assert len(sessions) == 1  # malformed one is skipped


def test_delete_removes_file(tmp_path):
    sid = new(sessions_dir=tmp_path)
    assert (tmp_path / f"{sid}.json").exists()
    assert delete(sid, sessions_dir=tmp_path) is True
    assert not (tmp_path / f"{sid}.json").exists()


def test_delete_returns_false_for_missing(tmp_path):
    assert delete("nope", sessions_dir=tmp_path) is False


def test_sessions_dir_created_if_missing(tmp_path):
    target = tmp_path / "deep" / "nested"
    sid = new(sessions_dir=target)
    assert (target / f"{sid}.json").exists()


def test_new_session_has_empty_messages_list(tmp_path):
    sid = new(sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert data is not None
    assert data["messages"] == []


def test_append_message_persists(tmp_path):
    sid = new(sessions_dir=tmp_path)
    ok = append_message(sid, "user", "hello world", sessions_dir=tmp_path)
    assert ok is True
    data = get(sid, sessions_dir=tmp_path)
    assert data is not None
    assert len(data["messages"]) == 1
    assert data["messages"][0]["role"] == "user"
    assert data["messages"][0]["content"] == "hello world"
    assert "ts" in data["messages"][0]


def test_append_message_returns_false_for_missing(tmp_path):
    assert append_message("nope", "user", "x", sessions_dir=tmp_path) is False


def test_append_message_ordered(tmp_path):
    sid = new(sessions_dir=tmp_path)
    append_message(sid, "user", "first", sessions_dir=tmp_path)
    append_message(sid, "assistant", "second", sessions_dir=tmp_path)
    append_message(sid, "user", "third", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert [m["content"] for m in data["messages"]] == ["first", "second", "third"]


def test_append_message_updates_updated_at(tmp_path):
    sid = new(sessions_dir=tmp_path)
    before = get(sid, sessions_dir=tmp_path)["updated_at"]
    import time as _t
    _t.sleep(0.01)
    append_message(sid, "user", "hi", sessions_dir=tmp_path)
    after = get(sid, sessions_dir=tmp_path)["updated_at"]
    assert after > before
