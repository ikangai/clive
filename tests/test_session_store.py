"""Tests for the persistent chat-session store."""
import json

from session_store import (
    new, get, list_sessions, delete, append_message, record_task,
    complete_last_task, list_sorted, most_recent, format_session_line,
    build_recap_text, run_task_in_session,
)


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


def test_new_session_has_empty_tasks_list(tmp_path):
    sid = new(sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert data["tasks"] == []


def test_record_task_persists(tmp_path):
    sid = new(sessions_dir=tmp_path)
    ok = record_task(sid, "list the files", summary="found 3", status="done",
                     sessions_dir=tmp_path)
    assert ok is True
    data = get(sid, sessions_dir=tmp_path)
    assert len(data["tasks"]) == 1
    t = data["tasks"][0]
    assert t["task"] == "list the files"
    assert t["summary"] == "found 3"
    assert t["status"] == "done"
    assert "started_at" in t


def test_record_task_returns_false_for_missing(tmp_path):
    assert record_task("nope", "do it", sessions_dir=tmp_path) is False


def test_record_task_auto_infers_title(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "list the files in /tmp", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert data["title"] == "list the files in /tmp"


def test_record_task_does_not_overwrite_explicit_title(tmp_path):
    sid = new(title="My Important Chat", sessions_dir=tmp_path)
    record_task(sid, "do something", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert data["title"] == "My Important Chat"


def test_record_task_title_truncated(tmp_path):
    sid = new(sessions_dir=tmp_path)
    long_task = "a" * 120
    record_task(sid, long_task, sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert len(data["title"]) <= 60
    assert data["title"].endswith("\u2026")


def test_record_task_multiple_tasks_append(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "task one", sessions_dir=tmp_path)
    record_task(sid, "task two", sessions_dir=tmp_path)
    record_task(sid, "task three", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert [t["task"] for t in data["tasks"]] == ["task one", "task two", "task three"]


def test_complete_last_task_sets_summary_and_status(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "list files", sessions_dir=tmp_path)
    ok = complete_last_task(sid, summary="found 5 files", status="done",
                            sessions_dir=tmp_path)
    assert ok is True
    data = get(sid, sessions_dir=tmp_path)
    last = data["tasks"][-1]
    assert last["summary"] == "found 5 files"
    assert last["status"] == "done"
    assert "completed_at" in last


def test_complete_last_task_only_updates_last(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "first", sessions_dir=tmp_path)
    record_task(sid, "second", sessions_dir=tmp_path)
    complete_last_task(sid, summary="second done", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert data["tasks"][0]["summary"] is None
    assert data["tasks"][0]["status"] == "pending"
    assert data["tasks"][1]["summary"] == "second done"
    assert data["tasks"][1]["status"] == "done"


def test_complete_last_task_failed_status(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "risky", sessions_dir=tmp_path)
    complete_last_task(sid, summary="oops", status="failed",
                       sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert data["tasks"][-1]["status"] == "failed"


def test_complete_last_task_returns_false_without_tasks(tmp_path):
    sid = new(sessions_dir=tmp_path)
    assert complete_last_task(sid, sessions_dir=tmp_path) is False


def test_complete_last_task_returns_false_for_missing(tmp_path):
    assert complete_last_task("nope", sessions_dir=tmp_path) is False


def test_complete_last_task_updates_updated_at(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "task", sessions_dir=tmp_path)
    before = get(sid, sessions_dir=tmp_path)["updated_at"]
    import time as _t
    _t.sleep(0.01)
    complete_last_task(sid, summary="done", sessions_dir=tmp_path)
    after = get(sid, sessions_dir=tmp_path)["updated_at"]
    assert after > before


def test_list_sorted_most_recent_first(tmp_path):
    import time as _t
    a = new(title="oldest", sessions_dir=tmp_path)
    _t.sleep(0.01)
    b = new(title="middle", sessions_dir=tmp_path)
    _t.sleep(0.01)
    c = new(title="newest", sessions_dir=tmp_path)
    sorted_list = list_sorted(sessions_dir=tmp_path)
    assert [s["id"] for s in sorted_list] == [c, b, a]


def test_list_sorted_empty(tmp_path):
    assert list_sorted(sessions_dir=tmp_path) == []


def test_list_sorted_updates_on_append(tmp_path):
    import time as _t
    a = new(title="a", sessions_dir=tmp_path)
    _t.sleep(0.01)
    b = new(title="b", sessions_dir=tmp_path)
    # b is newer after creation
    assert list_sorted(sessions_dir=tmp_path)[0]["id"] == b
    # Touching a via record_task should promote it
    _t.sleep(0.01)
    record_task(a, "new work", sessions_dir=tmp_path)
    assert list_sorted(sessions_dir=tmp_path)[0]["id"] == a


def test_most_recent_returns_latest(tmp_path):
    import time as _t
    new(title="one", sessions_dir=tmp_path)
    _t.sleep(0.01)
    latest = new(title="two", sessions_dir=tmp_path)
    assert most_recent(sessions_dir=tmp_path)["id"] == latest


def test_most_recent_none_when_empty(tmp_path):
    assert most_recent(sessions_dir=tmp_path) is None


def test_format_session_line_contains_fields(tmp_path):
    sid = new(title="hello world", sessions_dir=tmp_path)
    record_task(sid, "do stuff", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    line = format_session_line(data)
    assert sid in line
    assert "hello world" in line
    assert "1 tasks" in line


def test_format_session_line_untitled(tmp_path):
    sid = new(sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    line = format_session_line(data)
    assert "(untitled)" in line
    assert "0 tasks" in line


def test_build_recap_text_empty_session(tmp_path):
    sid = new(sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert build_recap_text(data) == ""


def test_build_recap_text_shows_title(tmp_path):
    sid = new(title="my chat", sessions_dir=tmp_path)
    record_task(sid, "first task", sessions_dir=tmp_path)
    complete_last_task(sid, summary="done 1", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    recap = build_recap_text(data)
    assert "my chat" in recap
    assert "first task" in recap
    assert "done 1" in recap


def test_build_recap_text_limits_to_last_n(tmp_path):
    # Explicit title so it doesn't collide with auto-inferred title
    # from the first task (which would sneak "step 0" into the header).
    sid = new(title="limit-test", sessions_dir=tmp_path)
    for i in range(5):
        record_task(sid, f"step {i}", sessions_dir=tmp_path)
        complete_last_task(sid, summary=f"outcome {i}", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    recap = build_recap_text(data, last_n=2)
    assert "step 3" in recap
    assert "step 4" in recap
    assert "step 0" not in recap
    assert "step 1" not in recap
    assert "2 of 5" in recap


def test_build_recap_text_includes_status(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "risky thing", sessions_dir=tmp_path)
    complete_last_task(sid, summary="kaboom", status="failed",
                       sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    recap = build_recap_text(data)
    assert "[failed]" in recap


def test_build_recap_text_pending_task_no_arrow(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "open task", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    recap = build_recap_text(data)
    assert "open task" in recap
    assert "[pending]" in recap
    assert "\u2192" not in recap  # no summary arrow when no summary


def test_build_recap_text_last_n_clamped(tmp_path):
    sid = new(sessions_dir=tmp_path)
    record_task(sid, "only", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    # last_n=0 should still show at least 1
    recap = build_recap_text(data, last_n=0)
    assert "only" in recap


def test_run_task_in_session_happy_path(tmp_path):
    sid = new(sessions_dir=tmp_path)
    runner = lambda task: f"ran: {task}"
    result = run_task_in_session(sid, "do the thing", runner,
                                 sessions_dir=tmp_path)
    assert result["status"] == "done"
    assert result["result"] == "ran: do the thing"
    assert result["error"] is None
    data = get(sid, sessions_dir=tmp_path)
    assert data["tasks"][-1]["status"] == "done"
    assert data["tasks"][-1]["summary"] == "ran: do the thing"


def test_run_task_in_session_catches_exceptions(tmp_path):
    sid = new(sessions_dir=tmp_path)
    def bad_runner(task):
        raise RuntimeError("boom")
    result = run_task_in_session(sid, "risky", bad_runner,
                                 sessions_dir=tmp_path)
    assert result["status"] == "failed"
    assert result["error"] == "boom"
    data = get(sid, sessions_dir=tmp_path)
    assert data["tasks"][-1]["status"] == "failed"
    assert data["tasks"][-1]["summary"] == "boom"


def test_run_task_in_session_missing_session(tmp_path):
    result = run_task_in_session("nope", "x", lambda t: "ok",
                                 sessions_dir=tmp_path)
    assert result["status"] == "no-session"
    assert "nope" in result["error"]


def test_run_task_in_session_none_result(tmp_path):
    sid = new(sessions_dir=tmp_path)
    result = run_task_in_session(sid, "void", lambda t: None,
                                 sessions_dir=tmp_path)
    assert result["status"] == "done"
    data = get(sid, sessions_dir=tmp_path)
    assert data["tasks"][-1]["summary"] is None


def test_run_task_in_session_multiple_tasks(tmp_path):
    sid = new(sessions_dir=tmp_path)
    run_task_in_session(sid, "a", lambda t: "A", sessions_dir=tmp_path)
    run_task_in_session(sid, "b", lambda t: "B", sessions_dir=tmp_path)
    run_task_in_session(sid, "c", lambda t: "C", sessions_dir=tmp_path)
    data = get(sid, sessions_dir=tmp_path)
    assert len(data["tasks"]) == 3
    assert [t["summary"] for t in data["tasks"]] == ["A", "B", "C"]
    assert all(t["status"] == "done" for t in data["tasks"])


def test_run_task_in_session_runner_receives_task(tmp_path):
    sid = new(sessions_dir=tmp_path)
    received = []
    run_task_in_session(sid, "capture me", lambda t: received.append(t) or "ok",
                        sessions_dir=tmp_path)
    assert received == ["capture me"]
