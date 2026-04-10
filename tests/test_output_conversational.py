"""Tests for conversational output mode (framed protocol)."""
import base64
import json

from output import (
    set_conversational, is_conversational,
    emit_turn, emit_context, emit_question, emit_file, emit_progress, emit_alive,
)


def _parse_frame(captured: str) -> tuple[str, dict]:
    line = captured.strip().splitlines()[-1]
    assert line.startswith("<<<CLIVE:") and line.endswith(">>>")
    body = line[len("<<<CLIVE:"):-len(">>>")]
    kind, b64 = body.split(":", 1)
    payload = json.loads(base64.b64decode(b64).decode())
    return kind, payload


def test_set_conversational():
    set_conversational(True)
    assert is_conversational()
    set_conversational(False)
    assert not is_conversational()


def test_emit_turn(capsys):
    emit_turn("done")
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "turn"
    assert payload == {"state": "done"}


def test_emit_context(capsys):
    emit_context({"result": "42"})
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "context"
    assert payload == {"result": "42"}


def test_emit_question(capsys):
    emit_question("which one?")
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "question"
    assert payload == {"text": "which one?"}


def test_emit_file(capsys):
    emit_file("out.txt")
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "file"
    assert payload == {"name": "out.txt"}


def test_emit_progress(capsys):
    emit_progress("step 1 of 3")
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "progress"
    assert payload == {"text": "step 1 of 3"}


def test_emit_alive_includes_timestamp(capsys):
    emit_alive()
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "alive"
    assert isinstance(payload["ts"], float)


# --- Legacy telemetry path (progress/step/detail/activity) ---
#
# When _conversational is on, these must emit framed `progress` frames so
# the outer parser can see them. When it is off, they must behave like
# before (printed to _stream()) — no framed output leaking to user-facing
# terminals.


def test_progress_conversational_emits_framed(capsys):
    from output import progress, set_conversational
    set_conversational(True)
    try:
        progress("step 1 of 3")
    finally:
        set_conversational(False)
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "progress"
    assert payload == {"text": "step 1 of 3"}


def test_progress_non_conversational_no_frame(capsys):
    from output import progress, set_conversational
    set_conversational(False)
    progress("step 1 of 3")
    out = capsys.readouterr().out
    assert "<<<CLIVE:" not in out
    assert "step 1 of 3" in out


def test_step_conversational_emits_framed(capsys):
    from output import step, set_conversational
    set_conversational(True)
    try:
        step("installing deps")
    finally:
        set_conversational(False)
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "progress"
    assert payload == {"text": "installing deps"}


def test_detail_conversational_emits_framed(capsys):
    from output import detail, set_conversational
    set_conversational(True)
    try:
        detail("  plan: 3 subtasks")
    finally:
        set_conversational(False)
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "progress"
    assert "plan: 3 subtasks" in payload["text"]


def test_activity_conversational_emits_framed(capsys):
    from output import activity, set_conversational
    set_conversational(True)
    try:
        activity("running shell")
    finally:
        set_conversational(False)
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "progress"
    assert payload == {"text": "running shell"}
