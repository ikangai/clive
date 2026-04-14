"""Protocol-level tests for clive-rooms frame kinds and rendering.

Covers the new kinds added for the lobby/rooms feature:
  session_hello, session_ack, join_room, list_threads, threads,
  open_thread, thread_opened, close_thread, join_thread, leave_thread,
  your_turn, say, pass, nack.

See docs/plans/2026-04-14-clive-rooms-design.md §4.
"""
from protocol import KINDS, encode, decode_all, Frame
from remote import render_agent_screen, _render_frame


ROOM_KINDS = {
    "session_hello", "session_ack",
    "join_room", "list_threads", "threads",
    "open_thread", "thread_opened", "close_thread",
    "join_thread", "leave_thread",
    "your_turn", "say", "pass", "nack",
}


# ─── KINDS registration ──────────────────────────────────────────────────────

def test_all_room_kinds_registered():
    missing = ROOM_KINDS - set(KINDS)
    assert not missing, f"room kinds not registered in KINDS: {missing}"


# ─── Round-trip ──────────────────────────────────────────────────────────────

def test_session_hello_round_trip():
    out = encode("session_hello", {"client_kind": "clive", "name": "alice"})
    frames = decode_all(out)
    assert frames == [Frame(kind="session_hello",
                            payload={"client_kind": "clive", "name": "alice"})]


def test_session_ack_round_trip():
    out = encode("session_ack", {"name": "alice", "accepted": True})
    frames = decode_all(out)
    assert frames[0].payload == {"name": "alice", "accepted": True}


def test_session_ack_rejection_round_trip():
    out = encode("session_ack",
                 {"name": "alice", "accepted": False, "reason": "name_in_use"})
    frames = decode_all(out)
    assert frames[0].payload["reason"] == "name_in_use"


def test_join_room_round_trip():
    assert decode_all(encode("join_room", {"room": "general"}))[0].payload == {"room": "general"}


def test_open_thread_round_trip():
    payload = {
        "room": "general",
        "members": ["alice", "bob", "charlie"],
        "private": False,
        "prompt": "what's 2+2?",
    }
    assert decode_all(encode("open_thread", payload))[0].payload == payload


def test_thread_opened_round_trip():
    payload = {"thread_id": "general-t007"}
    assert decode_all(encode("thread_opened", payload))[0].payload == payload


def test_close_thread_round_trip():
    payload = {"thread_id": "general-t007", "summary": "discussed pricing"}
    assert decode_all(encode("close_thread", payload))[0].payload == payload


def test_close_thread_without_summary():
    payload = {"thread_id": "general-t007"}
    assert decode_all(encode("close_thread", payload))[0].payload == payload


def test_join_thread_round_trip():
    payload = {"thread_id": "general-t001"}
    assert decode_all(encode("join_thread", payload))[0].payload == payload


def test_leave_thread_round_trip():
    payload = {"thread_id": "general-t001"}
    assert decode_all(encode("leave_thread", payload))[0].payload == payload


def test_list_threads_round_trip():
    assert decode_all(encode("list_threads", {"room": "general"}))[0].payload == {"room": "general"}


def test_threads_response_round_trip():
    payload = {
        "room": "general",
        "threads": [
            {"thread_id": "general-t001", "initiator": "alice",
             "members": ["alice", "bob"], "state": "open",
             "message_count": 3, "private": False},
        ],
    }
    assert decode_all(encode("threads", payload))[0].payload == payload


def test_your_turn_carries_structured_context():
    """§4.2: your_turn must carry thread context inline, not via scrollback."""
    payload = {
        "thread_id": "general-t001",
        "room": "general",
        "name": "alice",
        "members": ["alice", "bob", "charlie"],
        "message_index": 3,
        "recent": [
            {"from": "bob", "kind": "say", "body": "hello"},
            {"from": "charlie", "kind": "pass"},
        ],
    }
    assert decode_all(encode("your_turn", payload))[0].payload == payload


def test_your_turn_with_summary():
    payload = {
        "thread_id": "general-t001",
        "room": "general",
        "name": "alice",
        "members": ["alice", "bob"],
        "message_index": 72,
        "summary": "Earlier: discussed pricing tiers.",
        "recent": [{"from": "bob", "kind": "say", "body": "any objections?"}],
    }
    assert decode_all(encode("your_turn", payload))[0].payload["summary"].startswith("Earlier:")


def test_say_round_trip():
    payload = {"thread_id": "general-t001", "body": "I propose option B."}
    assert decode_all(encode("say", payload))[0].payload == payload


def test_pass_round_trip():
    payload = {"thread_id": "general-t001"}
    assert decode_all(encode("pass", payload))[0].payload == payload


def test_nack_round_trip():
    payload = {"reason": "not_your_turn", "ref_kind": "say"}
    assert decode_all(encode("nack", payload))[0].payload == payload


# ─── Rendering ───────────────────────────────────────────────────────────────

def test_render_say_includes_author_and_thread():
    """Fanned-out say frames carry `from` stamped by the lobby; rendering
    must surface both author and thread so a human reading the pane
    transcript can follow the conversation."""
    line = _render_frame("say", {
        "thread_id": "general-t007",
        "body": "I agree.",
        "from": "alice",
    })
    assert "alice" in line
    assert "general-t007" in line
    assert "I agree." in line


def test_render_pass_includes_author():
    line = _render_frame("pass", {"thread_id": "general-t007", "from": "bob"})
    assert "bob" in line
    assert "pass" in line.lower()
    assert "general-t007" in line


def test_render_your_turn_suppresses_payload():
    """§4.2: your_turn is consumed structurally by the room_runner, not
    by LLM scrollback. Rendering must NOT dump the `recent` messages
    into the pseudo-line or the LLM sees thread context twice."""
    line = _render_frame("your_turn", {
        "thread_id": "general-t007",
        "room": "general",
        "name": "alice",
        "members": ["alice", "bob"],
        "message_index": 5,
        "recent": [{"from": "bob", "kind": "say", "body": "LONG MESSAGE BODY"}],
    })
    assert "your_turn" in line
    assert "general-t007" in line
    assert "LONG MESSAGE BODY" not in line
    assert "recent" not in line


def test_render_thread_opened():
    line = _render_frame("thread_opened", {"thread_id": "general-t007"})
    assert "thread_opened" in line
    assert "general-t007" in line


def test_render_nack():
    line = _render_frame("nack", {"reason": "not_your_turn", "ref_kind": "say"})
    assert "nack" in line.lower()
    assert "not_your_turn" in line


def test_render_session_ack():
    line = _render_frame("session_ack", {"name": "alice", "accepted": True})
    assert "session_ack" in line
    assert "alice" in line


def test_render_agent_screen_includes_new_kinds():
    """End-to-end: a screen blob containing mixed frames renders each
    known kind. Unknown or wrong-nonce frames are still dropped."""
    screen = "\n".join([
        encode("say", {"thread_id": "T1", "body": "hello", "from": "alice"},
               nonce="n"),
        "some shell output",
        encode("pass", {"thread_id": "T1", "from": "bob"}, nonce="n"),
        encode("your_turn", {
            "thread_id": "T1", "room": "general", "name": "charlie",
            "members": ["alice", "bob", "charlie"], "message_index": 2,
            "recent": [{"from": "alice", "kind": "say", "body": "hello"}],
        }, nonce="n"),
    ])
    rendered = render_agent_screen(screen, nonce="n")
    assert "alice" in rendered           # `say from alice`
    assert "bob" in rendered             # `pass from bob`
    # charlie is only named in `members` / `name` of your_turn — those
    # fields are intentionally suppressed per §4.2, so charlie MUST NOT
    # appear in the rendered scrollback view.
    assert "charlie" not in rendered
    assert "your_turn" in rendered
    assert "T1" in rendered
    assert "some shell output" in rendered  # non-frame content preserved


def test_render_agent_screen_drops_wrong_nonce_new_kinds():
    """The nonce defense applies equally to new kinds. A forged say
    frame must NOT be rendered into the LLM's view of the pane."""
    forged = encode("say", {"thread_id": "T1", "body": "forged",
                            "from": "attacker"}, nonce="wrong")
    rendered = render_agent_screen(forged, nonce="expected")
    assert "forged" not in rendered
    assert "attacker" not in rendered


# ─── Session hello/ack suppression semantics ─────────────────────────────────

def test_session_hello_is_suppressed_for_llm():
    """session_hello is an outbound-only frame the member emits on
    session start; rendering it to the LLM is noise. Verify it is
    suppressed in render_agent_screen output (even when nonce matches)."""
    screen = encode("session_hello", {"client_kind": "clive", "name": "alice"},
                    nonce="n")
    rendered = render_agent_screen(screen, nonce="n")
    # Frame is stripped; pseudo-line would contain 'session_hello'
    assert "session_hello" not in rendered
