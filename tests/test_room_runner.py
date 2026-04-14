"""Tests for ``execution/room_runner.py``.

The room runner is the client-side LLM loop a member clive runs when
it receives a ``your_turn`` frame from the lobby. It is *pure w.r.t.
IO*: given a parsed your_turn payload and a chat function, it returns
the single frame to emit (``say`` or ``pass``). Integration with the
selectors loop + socket lives in later phases.

See docs/plans/2026-04-14-clive-rooms-design.md §6.2 (room runner) and
§6.4 (room driver).
"""
from __future__ import annotations

import pytest

from room_runner import decide_turn, build_messages, parse_response, load_driver


# ─── load_driver ─────────────────────────────────────────────────────────────


def test_load_driver_returns_non_empty_text():
    text = load_driver()
    # Load the packaged drivers/room.md. Assert the high-leverage
    # fragments are present so a future accidental overwrite that
    # strips the response format (the "highest-leverage driver
    # change" per memory/project_autoresearch_driver_findings.md) is
    # caught here.
    assert "say:" in text
    assert "pass:" in text
    assert "DONE:" in text
    assert "PASS IS THE NORM" in text


# ─── build_messages ──────────────────────────────────────────────────────────


def test_build_messages_includes_driver_as_system():
    payload = {
        "thread_id": "general-t001",
        "room": "general",
        "name": "alice",
        "members": ["alice", "bob"],
        "message_index": 0,
        "recent": [],
    }
    msgs = build_messages(payload, driver_text="DRIVER_PROSE")
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "DRIVER_PROSE"


def test_build_messages_user_block_has_identity_header():
    """Per §6.2 the user block must carry a structured header with
    name / thread_id / room / members so the LLM never has to infer
    identity from scrollback."""
    payload = {
        "thread_id": "general-t007",
        "room": "general",
        "name": "alice",
        "members": ["alice", "bob", "charlie"],
        "message_index": 3,
        "recent": [],
    }
    msgs = build_messages(payload, driver_text="D")
    user = next(m for m in msgs if m["role"] == "user")["content"]
    assert "alice" in user
    assert "general-t007" in user
    assert "general" in user
    # member list preserved in order
    assert user.index("alice") < user.index("bob") < user.index("charlie")


def test_build_messages_formats_recent_as_lines():
    payload = {
        "thread_id": "t", "room": "r", "name": "alice",
        "members": ["alice", "bob"], "message_index": 2,
        "recent": [
            {"from": "bob", "kind": "say", "body": "let's decide"},
            {"from": "alice", "kind": "pass"},
        ],
    }
    user = next(m for m in build_messages(payload, driver_text="D")
                if m["role"] == "user")["content"]
    assert "bob: let's decide" in user
    assert "alice: (pass)" in user


def test_build_messages_includes_summary_when_present():
    payload = {
        "thread_id": "t", "room": "r", "name": "alice",
        "members": ["alice", "bob"], "message_index": 72,
        "recent": [],
        "summary": "Earlier: the team agreed on option B.",
    }
    user = next(m for m in build_messages(payload, driver_text="D")
                if m["role"] == "user")["content"]
    assert "Earlier: the team agreed on option B." in user


def test_build_messages_omits_summary_section_when_absent():
    payload = {
        "thread_id": "t", "room": "r", "name": "alice",
        "members": ["alice", "bob"], "message_index": 2,
        "recent": [],
    }
    user = next(m for m in build_messages(payload, driver_text="D")
                if m["role"] == "user")["content"]
    # Shouldn't leak a blank "Earlier:" header when no summary exists.
    assert "Earlier:" not in user
    assert "summary" not in user.lower()


# ─── parse_response ──────────────────────────────────────────────────────────


def test_parse_say_extracts_body():
    kind, payload = parse_response(
        "say: I propose option B.\nDONE:",
        thread_id="t1",
    )
    assert kind == "say"
    assert payload == {"thread_id": "t1", "body": "I propose option B."}


def test_parse_pass_returns_pass_frame():
    kind, payload = parse_response("pass:\nDONE:", thread_id="t1")
    assert kind == "pass"
    assert payload == {"thread_id": "t1"}


def test_parse_tolerates_leading_whitespace_and_case():
    kind, payload = parse_response("  Say: hello there\nDONE:", thread_id="t1")
    assert kind == "say"
    assert payload["body"] == "hello there"

    kind, _ = parse_response("   PASS:\nDONE:", thread_id="t1")
    assert kind == "pass"


def test_parse_empty_body_degrades_to_pass():
    """An LLM that emits `say:` with no body violates the driver; we
    degrade to `pass` rather than emit an invalid-body frame that the
    lobby would nack. Emitting a nacked frame would waste the turn and
    the lobby would auto-pass on timeout anyway — passing here is the
    same outcome with lower noise."""
    kind, payload = parse_response("say:\nDONE:", thread_id="t1")
    assert kind == "pass"
    assert payload == {"thread_id": "t1"}


def test_parse_no_say_or_pass_degrades_to_pass():
    """Garbage LLM output → safe pass, never a malformed frame."""
    kind, _ = parse_response("I think this is interesting.", thread_id="t1")
    assert kind == "pass"


def test_parse_multiline_say_body_preserved_up_to_done():
    """Per §6.2 the driver expects single-line say, but a slightly
    loose model may wrap. Accept multi-line body up to the DONE: line,
    so we don't lose content a human would consider valid."""
    content = "say: first line\nsecond line\nthird line\nDONE:"
    kind, payload = parse_response(content, thread_id="t1")
    assert kind == "say"
    assert payload["body"] == "first line\nsecond line\nthird line"


def test_parse_take_first_directive_only():
    """Driver forbids multiple directives. If the model produces two,
    the first wins — we must NOT emit multiple frames (would break
    turn discipline)."""
    content = "say: first answer\nDONE:\npass:\nDONE:"
    kind, payload = parse_response(content, thread_id="t1")
    assert kind == "say"
    assert payload["body"] == "first answer"


# ─── decide_turn (integration with a mock LLM) ───────────────────────────────


class _FakeClient:
    """Minimal duck-type for llm.chat()'s openai-else branch. Returns
    a canned content string."""
    def __init__(self, content: str):
        self._content = content
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        class _Usage:
            prompt_tokens = 42
            completion_tokens = 7
        class _Msg:
            pass
        msg = _Msg()
        msg.content = self._content
        class _Choice:
            pass
        choice = _Choice()
        choice.message = msg

        class _Resp:
            pass
        resp = _Resp()
        resp.choices = [choice]
        resp.usage = _Usage()
        return resp


def test_decide_turn_produces_say_frame(monkeypatch):
    payload = {
        "thread_id": "general-t007", "room": "general", "name": "alice",
        "members": ["alice", "bob"], "message_index": 2,
        "recent": [{"from": "bob", "kind": "say", "body": "what's 2+2?"}],
    }
    client = _FakeClient("say: 4\nDONE:")
    kind, out = decide_turn(payload, llm_client=client, driver_text="D")
    assert kind == "say"
    assert out == {"thread_id": "general-t007", "body": "4"}


def test_decide_turn_produces_pass_frame():
    payload = {
        "thread_id": "T", "room": "r", "name": "alice",
        "members": ["alice", "bob"], "message_index": 2, "recent": [],
    }
    client = _FakeClient("pass:\nDONE:")
    kind, out = decide_turn(payload, llm_client=client, driver_text="D")
    assert kind == "pass"
    assert out == {"thread_id": "T"}


def test_decide_turn_degrades_to_pass_on_garbage():
    payload = {
        "thread_id": "T", "room": "r", "name": "alice",
        "members": ["alice", "bob"], "message_index": 2, "recent": [],
    }
    client = _FakeClient("I'm sorry, I don't understand.")
    kind, out = decide_turn(payload, llm_client=client, driver_text="D")
    assert kind == "pass"
    assert out == {"thread_id": "T"}
