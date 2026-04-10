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
