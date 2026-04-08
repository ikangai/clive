"""Tests for remote agent communication."""
from remote import (
    parse_remote_result, parse_remote_progress, parse_remote_files,
    build_remote_command, parse_turn_state, parse_context,
)


# ─── Protocol parsing ─────────────────────────────────────────────────────────

def test_parse_done_json():
    screen = 'some output\nDONE: {"status": "success", "result": "Found 47 records"}\n$'
    result = parse_remote_result(screen)
    assert result is not None
    assert result["status"] == "success"
    assert "47 records" in result["result"]


def test_parse_done_error():
    screen = 'DONE: {"status": "error", "reason": "connection refused"}'
    result = parse_remote_result(screen)
    assert result["status"] == "error"
    assert "connection refused" in result["reason"]


def test_parse_done_plain_text():
    screen = "DONE: task completed successfully"
    result = parse_remote_result(screen)
    assert result is not None
    assert result["result"] == "task completed successfully"


def test_parse_no_done():
    screen = "still running...\nprocessing..."
    assert parse_remote_result(screen) is None


def test_parse_progress():
    screen = "PROGRESS: step 1 of 3 — extracting data\nPROGRESS: step 2 of 3 — filtering\nsome output"
    progress = parse_remote_progress(screen)
    assert len(progress) == 2
    assert "extracting" in progress[0]
    assert "filtering" in progress[1]


def test_parse_files():
    screen = "FILE: result.csv\nFILE: report.json\nDONE: {\"status\": \"success\"}"
    files = parse_remote_files(screen)
    assert "result.csv" in files
    assert "report.json" in files


def test_parse_done_with_files():
    screen = 'DONE: {"status": "success", "result": "done", "files": ["data.csv", "report.pdf"]}'
    result = parse_remote_result(screen)
    assert result["files"] == ["data.csv", "report.pdf"]


# ─── Command building ─────────────────────────────────────────────────────────

def test_build_command_default():
    cmd = build_remote_command("check disk usage")
    assert "--quiet" in cmd
    assert "--json" in cmd
    assert "check disk usage" in cmd


def test_build_command_with_toolset():
    cmd = build_remote_command("browse web", toolset="standard")
    assert "-t standard" in cmd


def test_build_command_no_json():
    cmd = build_remote_command("task", json_output=False)
    assert "--json" not in cmd
    assert "--quiet" in cmd


def test_build_command_escapes_quotes():
    cmd = build_remote_command("find files with 'TODO' comments")
    assert "TODO" in cmd
    # Should be properly escaped for shell
    assert "'" in cmd


# ─── Turn state parsing ──────────────────────────────────────────────────────

def test_parse_turn_thinking():
    screen = "PROGRESS: step 1\nTURN: thinking"
    assert parse_turn_state(screen) == "thinking"


def test_parse_turn_waiting():
    screen = 'QUESTION: "which one?"\nTURN: waiting'
    assert parse_turn_state(screen) == "waiting"


def test_parse_turn_done():
    screen = 'CONTEXT: {"result": "found it"}\nTURN: done'
    assert parse_turn_state(screen) == "done"


def test_parse_turn_failed():
    screen = 'CONTEXT: {"error": "timeout"}\nTURN: failed'
    assert parse_turn_state(screen) == "failed"


def test_parse_turn_none():
    """No TURN: line → None (still working or not conversational)."""
    screen = "some output\nstill running..."
    assert parse_turn_state(screen) is None


def test_parse_turn_latest_wins():
    """Multiple TURN: lines → last one wins."""
    screen = "TURN: thinking\nPROGRESS: step 2\nTURN: waiting"
    assert parse_turn_state(screen) == "waiting"


# ─── Context parsing ─────────────────────────────────────────────────────────

def test_parse_context_json():
    screen = 'CONTEXT: {"result": "hello", "files": ["a.txt"]}\nTURN: done'
    ctx = parse_context(screen)
    assert ctx["result"] == "hello"
    assert ctx["files"] == ["a.txt"]


def test_parse_context_last_wins():
    screen = 'CONTEXT: {"step": 1}\nCONTEXT: {"step": 2, "result": "final"}\nTURN: done'
    ctx = parse_context(screen)
    assert ctx["step"] == 2


def test_parse_context_none():
    screen = "no context here\nTURN: done"
    assert parse_context(screen) is None
