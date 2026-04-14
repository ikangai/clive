"""Pure state-machine tests for the lobby (rooms phase 2).

Each test constructs a LobbyState, registers one or more sessions,
drives the state machine with Frame instances, and asserts on the
returned Send list + resulting in-memory state. No IO, no sleeps, no
tmux, no network.

See docs/plans/2026-04-14-clive-rooms-design.md §3–§4.
"""
from protocol import Frame
from lobby_state import (
    LobbyState, Send, handle,
)


def _send(state: LobbyState, sid: int, kind: str, payload: dict,
          now: float = 0.0) -> list[Send]:
    return handle(state, sid, Frame(kind=kind, payload=payload), now=now)


def _hello(state: LobbyState, sid: int, name: str,
           client_kind: str = "clive") -> list[Send]:
    state.register_session(sid)
    return _send(state, sid, "session_hello",
                 {"client_kind": client_kind, "name": name})


def _bootstrap(state: LobbyState, room: str, *named_sessions: tuple[int, str]):
    """Register N sessions with names, all joined to ``room``."""
    for sid, name in named_sessions:
        _hello(state, sid, name)
        _send(state, sid, "join_room", {"room": room})


def _outbound_by_kind(sends: list[Send], kind: str) -> list[Send]:
    return [s for s in sends if s.kind == kind]


# ─── session_hello / session_ack ──────────────────────────────────────────────

def test_session_hello_accepts_and_claims_name():
    s = LobbyState()
    s.register_session(1)
    out = _send(s, 1, "session_hello", {"client_kind": "clive", "name": "alice"})
    assert out == [Send(1, "session_ack", {"name": "alice", "accepted": True})]
    assert s.sessions[1].name == "alice"
    assert s.sessions[1].client_kind == "clive"
    assert s.name_to_session["alice"] == 1


def test_session_hello_rejects_duplicate_name():
    s = LobbyState()
    _hello(s, 1, "alice")
    s.register_session(2)
    out = _send(s, 2, "session_hello", {"client_kind": "clive", "name": "alice"})
    assert len(out) == 1
    assert out[0].kind == "session_ack"
    assert out[0].payload["accepted"] is False
    assert out[0].payload["reason"] == "name_in_use"
    assert s.sessions[2].name == ""  # unchanged


def test_session_hello_rejects_invalid_client_kind():
    s = LobbyState()
    s.register_session(1)
    out = _send(s, 1, "session_hello", {"client_kind": "robot", "name": "x"})
    assert out[0].payload["accepted"] is False
    assert "client_kind" in out[0].payload["reason"]


def test_session_hello_idempotent_on_same_session():
    s = LobbyState()
    _hello(s, 1, "alice")
    out = _send(s, 1, "session_hello", {"client_kind": "clive", "name": "alice"})
    assert out[0].payload["accepted"] is True


def test_frame_before_session_hello_is_nacked():
    s = LobbyState()
    s.register_session(1)
    out = _send(s, 1, "join_room", {"room": "general"})
    assert len(out) == 1
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "session_hello_required"


def test_drop_session_frees_name():
    s = LobbyState()
    _hello(s, 1, "alice")
    s.drop_session(1)
    assert "alice" not in s.name_to_session
    # Name is now re-claimable by a different session.
    _hello(s, 2, "alice")
    assert s.name_to_session["alice"] == 2


# ─── join_room ────────────────────────────────────────────────────────────────

def test_join_room_adds_membership_silently():
    s = LobbyState()
    _hello(s, 1, "alice")
    out = _send(s, 1, "join_room", {"room": "general"})
    assert out == []
    assert "alice" in s.rooms["general"].member_names
    assert "general" in s.sessions[1].joined_rooms


def test_join_room_missing_room_field():
    s = LobbyState()
    _hello(s, 1, "alice")
    out = _send(s, 1, "join_room", {})
    assert out[0].kind == "nack"


# ─── open_thread validation ───────────────────────────────────────────────────

def test_open_thread_requires_initiator_in_room():
    s = LobbyState()
    _hello(s, 1, "alice")
    s.ensure_room("general")
    out = _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice"], "private": False, "prompt": "hi",
    })
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "not_in_room"


def test_open_thread_requires_initiator_first_member():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"), (2, "bob"))
    out = _send(s, 1, "open_thread", {
        "room": "general", "members": ["bob", "alice"], "private": False, "prompt": "",
    })
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "initiator_must_be_first"


def test_open_thread_rejects_members_not_in_room():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"))
    out = _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "ghost"], "private": False, "prompt": "",
    })
    assert out[0].kind == "nack"
    assert out[0].payload["reason"].startswith("members_not_in_room")


def test_open_thread_rejects_duplicate_members():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"), (2, "bob"))
    out = _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob", "alice"],
        "private": False, "prompt": "",
    })
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "duplicate_members"


# ─── open_thread happy paths ──────────────────────────────────────────────────

def test_open_thread_with_no_prompt_grants_initiator_turn():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"), (2, "bob"))
    out = _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob"], "private": False, "prompt": "",
    })
    opened = _outbound_by_kind(out, "thread_opened")
    turn = _outbound_by_kind(out, "your_turn")
    assert len(opened) == 1
    assert opened[0].payload["thread_id"] == "general-t001"
    assert len(turn) == 1
    assert turn[0].session_id == 1  # alice receives the turn
    assert turn[0].payload["name"] == "alice"
    assert turn[0].payload["members"] == ["alice", "bob"]
    assert turn[0].payload["recent"] == []


def test_open_thread_with_prompt_fans_out_and_grants_second_member_turn():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"), (2, "bob"), (3, "charlie"))
    out = _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob", "charlie"],
        "private": False, "prompt": "opening move",
    })
    # alice gets thread_opened, bob+charlie get the fanned-out `say`,
    # bob (cursor advanced to 1) gets `your_turn`.
    opened = _outbound_by_kind(out, "thread_opened")
    says = _outbound_by_kind(out, "say")
    turns = _outbound_by_kind(out, "your_turn")
    assert len(opened) == 1
    assert {s.session_id for s in says} == {2, 3}
    assert all(s.payload["from"] == "alice" for s in says)
    assert all(s.payload["body"] == "opening move" for s in says)
    assert len(turns) == 1
    assert turns[0].session_id == 2  # bob
    assert turns[0].payload["name"] == "bob"
    # your_turn carries the opening prompt in recent
    assert turns[0].payload["recent"] == [
        {"from": "alice", "kind": "say", "body": "opening move"}
    ]


def test_open_thread_generates_monotonic_ids_per_room():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"))
    out1 = _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice"], "private": False, "prompt": "",
    })
    out2 = _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice"], "private": False, "prompt": "",
    })
    assert _outbound_by_kind(out1, "thread_opened")[0].payload["thread_id"] == "general-t001"
    assert _outbound_by_kind(out2, "thread_opened")[0].payload["thread_id"] == "general-t002"


# ─── say / pass / rotation ────────────────────────────────────────────────────

def _open_thread_abc(s: LobbyState, prompt: str = "start") -> str:
    """Helper: open a thread with alice, bob, charlie."""
    _bootstrap(s, "general", (1, "alice"), (2, "bob"), (3, "charlie"))
    out = _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob", "charlie"],
        "private": False, "prompt": prompt,
    })
    return _outbound_by_kind(out, "thread_opened")[0].payload["thread_id"]


def test_say_out_of_turn_is_nacked():
    s = LobbyState()
    tid = _open_thread_abc(s)
    # It's bob's turn (cursor advanced past alice after the prompt).
    # charlie attempts to say → nack.
    out = _send(s, 3, "say", {"thread_id": tid, "body": "me first"})
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "not_your_turn"


def test_say_current_speaker_advances_rotation():
    s = LobbyState()
    tid = _open_thread_abc(s)
    # bob speaks.
    out = _send(s, 2, "say", {"thread_id": tid, "body": "bob here"})
    says = _outbound_by_kind(out, "say")
    turns = _outbound_by_kind(out, "your_turn")
    # Fan-out to alice + charlie, your_turn to charlie.
    assert {s.session_id for s in says} == {1, 3}
    assert len(turns) == 1 and turns[0].session_id == 3
    assert turns[0].payload["recent"][-1] == {"from": "bob", "kind": "say", "body": "bob here"}


def test_pass_advances_rotation():
    s = LobbyState()
    tid = _open_thread_abc(s)
    out = _send(s, 2, "pass", {"thread_id": tid})
    passes = _outbound_by_kind(out, "pass")
    turns = _outbound_by_kind(out, "your_turn")
    assert {s.session_id for s in passes} == {1, 3}
    assert len(turns) == 1 and turns[0].session_id == 3


def test_consecutive_passes_reach_quiescence():
    """A full rotation of passes → thread dormant, no further your_turn."""
    s = LobbyState()
    tid = _open_thread_abc(s, prompt="")  # no prompt → alice starts
    # alice pass, bob pass, charlie pass → cursor back at alice → dormant.
    _send(s, 1, "pass", {"thread_id": tid})
    _send(s, 2, "pass", {"thread_id": tid})
    out = _send(s, 3, "pass", {"thread_id": tid})
    assert s.threads[tid].state == "dormant"
    # No new your_turn should be emitted on the closing pass.
    assert _outbound_by_kind(out, "your_turn") == []


def test_say_resets_pass_streak():
    s = LobbyState()
    tid = _open_thread_abc(s, prompt="")
    _send(s, 1, "pass", {"thread_id": tid})
    _send(s, 2, "say", {"thread_id": tid, "body": "hold on"})
    # After bob's say, consecutive_passes reset; thread must stay open.
    assert s.threads[tid].state == "open"
    assert s.threads[tid].consecutive_passes == 0


def test_say_on_closed_thread_is_nacked():
    s = LobbyState()
    tid = _open_thread_abc(s)
    # Alice closes the thread.
    _send(s, 1, "close_thread", {"thread_id": tid})
    out = _send(s, 2, "say", {"thread_id": tid, "body": "wait"})
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "thread_closed"


def test_say_body_required():
    s = LobbyState()
    tid = _open_thread_abc(s)
    out = _send(s, 2, "say", {"thread_id": tid, "body": ""})
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "invalid_body"


# ─── list_threads & private visibility ───────────────────────────────────────

def test_list_threads_public_visible_to_all_room_members():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"), (2, "bob"))
    _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob"], "private": False,
        "prompt": "",
    })
    out = _send(s, 2, "list_threads", {"room": "general"})
    resp = _outbound_by_kind(out, "threads")[0]
    assert len(resp.payload["threads"]) == 1
    assert resp.payload["threads"][0]["thread_id"] == "general-t001"


def test_list_threads_hides_private_from_non_members():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"), (2, "bob"), (3, "charlie"))
    # Private thread with alice + bob; charlie is NOT a member.
    _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob"], "private": True,
        "prompt": "",
    })
    # Member sees it.
    out_bob = _send(s, 2, "list_threads", {"room": "general"})
    assert len(_outbound_by_kind(out_bob, "threads")[0].payload["threads"]) == 1
    # Non-member does NOT.
    out_charlie = _send(s, 3, "list_threads", {"room": "general"})
    assert _outbound_by_kind(out_charlie, "threads")[0].payload["threads"] == []


# ─── join_thread / leave_thread / close_thread ───────────────────────────────

def test_join_thread_public_appends_at_end():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"), (2, "bob"), (3, "charlie"))
    _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob"], "private": False,
        "prompt": "",
    })
    _send(s, 3, "join_thread", {"thread_id": "general-t001"})
    assert s.threads["general-t001"].members == ["alice", "bob", "charlie"]


def test_join_thread_private_is_rejected():
    s = LobbyState()
    _bootstrap(s, "general", (1, "alice"), (2, "bob"), (3, "charlie"))
    _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob"], "private": True,
        "prompt": "",
    })
    out = _send(s, 3, "join_thread", {"thread_id": "general-t001"})
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "private_thread"


def test_close_thread_requires_initiator():
    s = LobbyState()
    tid = _open_thread_abc(s)
    out = _send(s, 2, "close_thread", {"thread_id": tid})
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "not_initiator"


def test_close_thread_by_initiator_succeeds():
    s = LobbyState()
    tid = _open_thread_abc(s)
    out = _send(s, 1, "close_thread", {"thread_id": tid})
    assert out == []
    assert s.threads[tid].state == "closed"


def test_leave_thread_transfers_initiator_ownership():
    s = LobbyState()
    tid = _open_thread_abc(s, prompt="")
    _send(s, 1, "leave_thread", {"thread_id": tid})
    assert s.threads[tid].initiator == "bob"
    assert s.threads[tid].members == ["bob", "charlie"]


def test_leave_thread_adjusts_cursor():
    """If the member leaving is before the cursor, cursor shifts left."""
    s = LobbyState()
    tid = _open_thread_abc(s)  # cursor at bob (idx 1) after alice's prompt
    # alice (idx 0) leaves — cursor 1 → 0 (now points to bob, who is now at 0)
    _send(s, 1, "leave_thread", {"thread_id": tid})
    assert s.threads[tid].current_speaker == "bob"


# ─── Humans ──────────────────────────────────────────────────────────────────

def test_human_say_bypasses_turn_and_resets_cursor():
    s = LobbyState()
    # Three clives + one human in the same room.
    _bootstrap(s, "general", (1, "alice"), (2, "bob"), (3, "charlie"))
    _hello(s, 10, "helen", client_kind="human")
    _send(s, 10, "join_room", {"room": "general"})
    # Alice opens a thread with alice+bob+charlie (helen not in members).
    _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice", "bob", "charlie"],
        "private": False, "prompt": "start",
    })
    # Cursor now at bob (idx 1). Helen injects.
    out = _send(s, 10, "say", {"thread_id": "general-t001", "body": "redirect!"})
    says = _outbound_by_kind(out, "say")
    turns = _outbound_by_kind(out, "your_turn")
    # Fanout reaches alice, bob, charlie (all thread members).
    assert {s.session_id for s in says} == {1, 2, 3}
    assert all(s.payload["from"] == "helen" for s in says)
    # Cursor reset to 0 → alice receives your_turn.
    assert len(turns) == 1 and turns[0].session_id == 1


def test_human_pass_is_rejected():
    s = LobbyState()
    _hello(s, 10, "helen", client_kind="human")
    _bootstrap(s, "general", (1, "alice"))
    _send(s, 10, "join_room", {"room": "general"})
    _send(s, 1, "open_thread", {
        "room": "general", "members": ["alice"], "private": False, "prompt": "",
    })
    out = _send(s, 10, "pass", {"thread_id": "general-t001"})
    assert out[0].kind == "nack"
    assert out[0].payload["reason"] == "humans_do_not_pass"


# ─── recent window ───────────────────────────────────────────────────────────

def test_recent_window_caps_at_K():
    """your_turn.recent must be capped at recent_window (default 50)."""
    s = LobbyState()
    s.recent_window = 3  # shrink for test clarity
    tid = _open_thread_abc(s, prompt="m0")
    # Drive several rotations of say. Each say's your_turn carries the
    # last N=3 messages only.
    for i in range(5):
        speaker_session = ((i + 1) % 3) + 1  # bob=2, charlie=3, alice=1, ...
        _send(s, speaker_session, "say",
              {"thread_id": tid, "body": f"m{i+1}"})
    # Next your_turn (emitted last) should have only the 3 most recent.
    t = s.threads[tid]
    assert len(t.messages) == 6
    # Simulate another say to check the outbound your_turn payload.
    speaker_session = (6 % 3) + 1
    out = _send(s, speaker_session, "say", {"thread_id": tid, "body": "m6"})
    turn = _outbound_by_kind(out, "your_turn")[0]
    assert len(turn.payload["recent"]) == 3
    # And it's the TAIL — last three messages.
    recent_bodies = [e["body"] for e in turn.payload["recent"]]
    assert recent_bodies == ["m4", "m5", "m6"]
