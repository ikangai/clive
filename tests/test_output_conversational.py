"""Tests for conversational output mode."""
import io
import sys
from output import (
    set_conversational, is_conversational,
    emit_turn, emit_context, emit_question,
)


def test_set_conversational():
    set_conversational(True)
    assert is_conversational()
    set_conversational(False)
    assert not is_conversational()


def test_emit_turn(capsys):
    set_conversational(True)
    try:
        emit_turn("thinking")
        captured = capsys.readouterr()
        assert "TURN: thinking" in captured.out
    finally:
        set_conversational(False)


def test_emit_context(capsys):
    set_conversational(True)
    try:
        emit_context({"result": "hello"})
        captured = capsys.readouterr()
        assert "CONTEXT:" in captured.out
        assert '"result"' in captured.out
    finally:
        set_conversational(False)


def test_emit_question(capsys):
    set_conversational(True)
    try:
        emit_question("Which one do you want?")
        captured = capsys.readouterr()
        assert 'QUESTION: "Which one do you want?"' in captured.out
    finally:
        set_conversational(False)
