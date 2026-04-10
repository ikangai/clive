"""Tests for remote agent communication (framed protocol)."""
from protocol import encode
from remote import (
    parse_remote_progress, parse_remote_files,
    build_remote_command, parse_turn_state, parse_context,
    parse_question,
)


# ─── Turn state parsing ──────────────────────────────────────────────────────

def test_parse_turn_state_thinking():
    screen = "some shell output\n" + encode("turn", {"state": "thinking"}) + "\n"
    assert parse_turn_state(screen) == "thinking"


def test_parse_turn_state_waiting():
    screen = encode("question", {"text": "which one?"}) + "\n" + encode("turn", {"state": "waiting"})
    assert parse_turn_state(screen) == "waiting"


def test_parse_turn_state_done():
    screen = encode("context", {"result": "found it"}) + "\n" + encode("turn", {"state": "done"})
    assert parse_turn_state(screen) == "done"


def test_parse_turn_state_failed():
    screen = encode("context", {"error": "timeout"}) + "\n" + encode("turn", {"state": "failed"})
    assert parse_turn_state(screen) == "failed"


def test_parse_turn_state_last_wins():
    screen = "\n".join([
        encode("turn", {"state": "thinking"}),
        encode("turn", {"state": "waiting"}),
    ])
    assert parse_turn_state(screen) == "waiting"


def test_parse_turn_state_none():
    assert parse_turn_state("just shell output\n") is None


# ─── Context parsing ─────────────────────────────────────────────────────────

def test_parse_context_json():
    screen = encode("context", {"result": "42"})
    assert parse_context(screen) == {"result": "42"}


def test_parse_context_last_wins():
    screen = "\n".join([
        encode("context", {"result": "old"}),
        encode("context", {"result": "new"}),
    ])
    assert parse_context(screen) == {"result": "new"}


def test_parse_context_none():
    assert parse_context("no frame here") is None


# ─── Files and progress ──────────────────────────────────────────────────────

def test_parse_remote_files():
    screen = "\n".join([
        encode("file", {"name": "a.txt"}),
        encode("file", {"name": "b.png"}),
    ])
    assert parse_remote_files(screen) == ["a.txt", "b.png"]


def test_parse_remote_progress():
    screen = "\n".join([
        encode("progress", {"text": "step 1"}),
        encode("progress", {"text": "step 2"}),
    ])
    assert parse_remote_progress(screen) == ["step 1", "step 2"]


# ─── Spoof protection ────────────────────────────────────────────────────────

def test_stray_sentinel_does_not_parse():
    # LLM output containing the literal string <<<CLIVE:turn:done>>> must be ignored.
    screen = "<<<CLIVE:turn:done>>>\n"
    assert parse_turn_state(screen) is None


# ─── Command building ────────────────────────────────────────────────────────

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


# ─── Legacy removal regression ───────────────────────────────────────────────

def test_parse_remote_result_no_longer_exported():
    import remote
    assert not hasattr(remote, "parse_remote_result")
