"""Lobby state machine — pure, deterministic, IO-free.

The lobby is the broker for clive rooms. Its core is a pure function:

    handle(state, session_id, frame, now) -> list[Send]

Given a current ``LobbyState``, an inbound ``Frame`` arriving from a
session, and an absolute timestamp, it mutates the state in place and
returns the outbound frames the IO layer should emit. No disk, no
network, no clocks — all injected. This is the property that makes the
trust-critical piece unit-testable exhaustively.

See ``docs/plans/2026-04-14-clive-rooms-design.md`` §3 (turn
discipline) and §4 (protocol) for the semantics this encodes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from protocol import Frame


# ─── Data model ──────────────────────────────────────────────────────────────

@dataclass
class Session:
    """An SSH session connected to the lobby. `id` is an opaque handle
    the IO layer hands us (typically a file descriptor or index). We
    track it so outbound frames can be routed back to the right pipe.

    Before a session sends ``session_hello`` the fields ``name`` and
    ``client_kind`` are empty strings, meaning "unnamed" — the session
    is accepted but cannot take any action other than ``session_hello``.
    """
    id: int
    name: str = ""
    client_kind: str = ""   # "clive" | "human" | ""
    joined_rooms: set[str] = field(default_factory=set)


@dataclass
class Message:
    """An entry in a thread's append-only log. ``body`` is None for a
    pass. ``ts`` is the absolute Unix timestamp the lobby accepted the
    frame."""
    sender: str
    kind: str        # "say" | "pass"
    body: Optional[str]
    ts: float


@dataclass
class Thread:
    thread_id: str
    room: str
    initiator: str
    members: list[str]        # ordered; position 0 is the initiator
    private: bool
    state: str = "open"       # "open" | "dormant" | "closed"
    cursor: int = 0           # index into members of the current speaker
    messages: list[Message] = field(default_factory=list)
    consecutive_passes: int = 0  # reset on any `say`; counts all-pass rotation

    @property
    def current_speaker(self) -> str:
        if not self.members:
            return ""
        return self.members[self.cursor]


@dataclass
class Room:
    name: str
    member_names: set[str] = field(default_factory=set)   # currently joined
    thread_ids: list[str] = field(default_factory=list)   # in creation order


@dataclass
class Send:
    """A single outbound frame the IO layer should deliver."""
    session_id: int
    kind: str
    payload: dict


@dataclass
class LobbyState:
    rooms: dict[str, Room] = field(default_factory=dict)
    threads: dict[str, Thread] = field(default_factory=dict)
    sessions: dict[int, Session] = field(default_factory=dict)
    # Reverse lookup: claimed name -> session id. Enforces uniqueness of
    # live names (§7.1).
    name_to_session: dict[str, int] = field(default_factory=dict)
    # Per-room monotonic counter used to synthesize thread_ids.
    _thread_counter: dict[str, int] = field(default_factory=dict)
    # Recent-messages window passed in `your_turn.recent` (§4.2).
    recent_window: int = 50

    def ensure_room(self, name: str) -> Room:
        """Create a room if missing. In production, rooms are defined in
        lobby.yaml — this lazily materializes one so the state machine
        can be tested without a config loader."""
        if name not in self.rooms:
            self.rooms[name] = Room(name=name)
        return self.rooms[name]

    def register_session(self, session_id: int) -> Session:
        """Called by the IO layer when a new SSH connection is accepted,
        *before* any frame is received."""
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(id=session_id)
        return self.sessions[session_id]

    def drop_session(self, session_id: int) -> None:
        """Called by the IO layer when the session's pipe closes. Frees
        the name back into the pool and removes room memberships. Does
        NOT touch threads — a member dropping mid-thread is handled by
        rotation/timeout logic (Phase 6)."""
        sess = self.sessions.pop(session_id, None)
        if sess is None:
            return
        if sess.name and self.name_to_session.get(sess.name) == session_id:
            del self.name_to_session[sess.name]
        for room_name in sess.joined_rooms:
            room = self.rooms.get(room_name)
            if room is not None:
                room.member_names.discard(sess.name)


# ─── Dispatch ────────────────────────────────────────────────────────────────

def handle(state: LobbyState, session_id: int, frame: Frame,
           now: float = 0.0) -> list[Send]:
    """Dispatch a single inbound frame. Returns the outbound frames to
    emit. State is mutated in place.

    Unknown or malformed frames produce a ``nack`` and no state change.
    """
    sess = state.sessions.get(session_id)
    if sess is None:
        # IO layer didn't register; this shouldn't happen in production
        # but we refuse rather than crash.
        return []

    kind = frame.kind
    p = frame.payload

    # session_hello is the only frame a nameless session may send.
    if kind == "session_hello":
        return _handle_session_hello(state, sess, p)
    if not sess.name:
        return [_nack(session_id, "session_hello_required", kind)]

    if kind == "join_room":
        return _handle_join_room(state, sess, p)
    if kind == "open_thread":
        return _handle_open_thread(state, sess, p, now)
    if kind == "say":
        return _handle_say(state, sess, p, now)
    if kind == "pass":
        return _handle_pass(state, sess, p, now)
    if kind == "list_threads":
        return _handle_list_threads(state, sess, p)
    if kind == "join_thread":
        return _handle_join_thread(state, sess, p, now)
    if kind == "leave_thread":
        return _handle_leave_thread(state, sess, p)
    if kind == "close_thread":
        return _handle_close_thread(state, sess, p)
    # Any other kind is silently accepted (alive) or rejected (unknown).
    if kind == "alive":
        return []
    return [_nack(session_id, "unknown_kind", kind)]


# ─── Handlers ────────────────────────────────────────────────────────────────

def _handle_session_hello(state: LobbyState, sess: Session, p: dict) -> list[Send]:
    name = p.get("name")
    client_kind = p.get("client_kind")
    if not isinstance(name, str) or not name:
        return [_ack_reject(sess.id, "", "invalid_name")]
    if client_kind not in ("clive", "human"):
        return [_ack_reject(sess.id, name, "invalid_client_kind")]
    # Name uniqueness
    existing = state.name_to_session.get(name)
    if existing is not None and existing != sess.id:
        return [_ack_reject(sess.id, name, "name_in_use")]
    # Re-hello on the same session: allow idempotently.
    sess.name = name
    sess.client_kind = client_kind
    state.name_to_session[name] = sess.id
    return [Send(sess.id, "session_ack", {"name": name, "accepted": True})]


def _handle_join_room(state: LobbyState, sess: Session, p: dict) -> list[Send]:
    room_name = p.get("room")
    if not isinstance(room_name, str) or not room_name:
        return [_nack(sess.id, "invalid_room", "join_room")]
    room = state.ensure_room(room_name)
    room.member_names.add(sess.name)
    sess.joined_rooms.add(room_name)
    return []  # join is silent on success; list_threads/your_turn follow naturally


def _handle_open_thread(state: LobbyState, sess: Session, p: dict,
                        now: float) -> list[Send]:
    room_name = p.get("room")
    members = p.get("members")
    private = bool(p.get("private", False))
    prompt = p.get("prompt", "")

    if not isinstance(room_name, str) or room_name not in state.rooms:
        return [_nack(sess.id, "invalid_room", "open_thread")]
    if sess.name not in state.rooms[room_name].member_names:
        return [_nack(sess.id, "not_in_room", "open_thread")]
    if not isinstance(members, list) or not members:
        return [_nack(sess.id, "invalid_members", "open_thread")]
    if not all(isinstance(m, str) and m for m in members):
        return [_nack(sess.id, "invalid_members", "open_thread")]
    if members[0] != sess.name:
        return [_nack(sess.id, "initiator_must_be_first", "open_thread")]
    # Humans are never in rotation (§3.3). Forbid them in `members`;
    # since members[0] is the initiator, this also forbids human-
    # initiated threads in v1 (deferred per design open items).
    for m in members:
        m_sid = state.name_to_session.get(m)
        if m_sid is not None and state.sessions[m_sid].client_kind == "human":
            return [_nack(sess.id, f"human_in_members:{m}", "open_thread")]
    # Every listed member must be a current room member (§4.4
    # validation). An offline/absent name would immediately stall the
    # rotation, so reject rather than silently accept.
    room = state.rooms[room_name]
    missing = [m for m in members if m not in room.member_names]
    if missing:
        return [_nack(sess.id, f"members_not_in_room:{','.join(missing)}",
                      "open_thread")]
    if len(set(members)) != len(members):
        return [_nack(sess.id, "duplicate_members", "open_thread")]
    if not isinstance(prompt, str):
        return [_nack(sess.id, "invalid_prompt", "open_thread")]

    # Assign a lobby-owned thread_id (§C4 fix).
    state._thread_counter[room_name] = state._thread_counter.get(room_name, 0) + 1
    thread_id = f"{room_name}-t{state._thread_counter[room_name]:03d}"
    thread = Thread(
        thread_id=thread_id,
        room=room_name,
        initiator=sess.name,
        members=list(members),
        private=private,
    )
    # The opening prompt is appended as the initiator's first `say`.
    # Cursor stays at 0 (initiator) until they emit the prompt implicitly.
    # Simpler model: the prompt IS the first message; cursor advances
    # to members[1] and they receive `your_turn`.
    if prompt:
        thread.messages.append(Message(sender=sess.name, kind="say",
                                       body=prompt, ts=now))
        thread.cursor = 1 % len(thread.members)
    state.threads[thread_id] = thread
    room.thread_ids.append(thread_id)

    out: list[Send] = [
        Send(sess.id, "thread_opened", {"thread_id": thread_id}),
    ]
    # Fanout the opening prompt to everyone else (if any prompt).
    if prompt:
        out.extend(_fanout_message(state, thread, sess.name, "say",
                                   body=prompt))
        # Notify the new current speaker (members[1]) that it's their turn.
        out.extend(_emit_your_turn(state, thread))
    else:
        # No prompt — initiator still holds cursor. Send your_turn so
        # they can issue a message as the first move.
        out.extend(_emit_your_turn(state, thread))
    return out


def _handle_say(state: LobbyState, sess: Session, p: dict,
                now: float) -> list[Send]:
    thread, err = _resolve_thread(state, sess, p, "say")
    if err:
        return [err]
    if sess.client_kind == "human":
        return _handle_human_say(state, sess, thread, p, now)
    if thread.current_speaker != sess.name:
        return [_nack(sess.id, "not_your_turn", "say")]
    body = p.get("body")
    if not isinstance(body, str) or not body.strip():
        return [_nack(sess.id, "invalid_body", "say")]
    thread.messages.append(Message(sender=sess.name, kind="say",
                                   body=body, ts=now))
    thread.consecutive_passes = 0
    out = _fanout_message(state, thread, sess.name, "say", body=body)
    _advance_cursor(thread)
    out.extend(_emit_your_turn(state, thread))
    return out


def _handle_pass(state: LobbyState, sess: Session, p: dict,
                 now: float) -> list[Send]:
    thread, err = _resolve_thread(state, sess, p, "pass")
    if err:
        return [err]
    if sess.client_kind == "human":
        # Humans never pass; they only initiate or inject (§3.3).
        return [_nack(sess.id, "humans_do_not_pass", "pass")]
    if thread.current_speaker != sess.name:
        return [_nack(sess.id, "not_your_turn", "pass")]
    thread.messages.append(Message(sender=sess.name, kind="pass",
                                   body=None, ts=now))
    thread.consecutive_passes += 1
    out = _fanout_message(state, thread, sess.name, "pass", body=None)
    _advance_cursor(thread)
    # Quiescence: len(members) consecutive passes means every rotation
    # participant has had a turn in the streak and passed. Do NOT gate
    # on `cursor == 0` — that's only true when the initial speaker was
    # the initiator, which is not the case for threads opened with a
    # prompt (cursor starts at members[1]). Phase 2 treats every member
    # as online; Phase 6 adds offline skipping.
    if thread.consecutive_passes >= len(thread.members):
        thread.state = "dormant"
        return out  # no your_turn emission — thread is idle
    out.extend(_emit_your_turn(state, thread))
    return out


def _handle_human_say(state: LobbyState, sess: Session, thread: Thread,
                      p: dict, now: float) -> list[Send]:
    """Humans bypass current_speaker and reset the rotation cursor so
    clives respond to the fresh prompt (§3.3). Private threads still
    require explicit membership even for humans — the fanout isolation
    in §7.1 is symmetric."""
    # Room-membership check lives in _resolve_thread now; by the time we
    # get here the human is confirmed in thread.room.
    if thread.private and sess.name not in thread.members:
        return [_nack(sess.id, "private_thread", "say")]
    body = p.get("body")
    if not isinstance(body, str) or not body.strip():
        return [_nack(sess.id, "invalid_body", "say")]
    thread.messages.append(Message(sender=sess.name, kind="say",
                                   body=body, ts=now))
    thread.consecutive_passes = 0
    # If the thread was dormant, a human prompt reopens it.
    if thread.state == "dormant":
        thread.state = "open"
    out = _fanout_message(state, thread, sess.name, "say", body=body)
    # Humans aren't in members (enforced at open_thread), so the cursor
    # simply resets to position 0.
    thread.cursor = 0
    if thread.members:
        out.extend(_emit_your_turn(state, thread))
    return out


def _handle_list_threads(state: LobbyState, sess: Session, p: dict) -> list[Send]:
    room_name = p.get("room")
    if not isinstance(room_name, str) or room_name not in state.rooms:
        return [_nack(sess.id, "invalid_room", "list_threads")]
    threads = []
    for tid in state.rooms[room_name].thread_ids:
        t = state.threads[tid]
        # Private threads are completely invisible to non-members (§7.1).
        if t.private and sess.name not in t.members:
            continue
        threads.append({
            "thread_id": t.thread_id,
            "initiator": t.initiator,
            "members": list(t.members),
            "state": t.state,
            "message_count": len(t.messages),
            "private": t.private,
        })
    return [Send(sess.id, "threads", {"room": room_name, "threads": threads})]


def _handle_join_thread(state: LobbyState, sess: Session, p: dict,
                        now: float) -> list[Send]:
    tid = p.get("thread_id")
    if not isinstance(tid, str) or tid not in state.threads:
        return [_nack(sess.id, "invalid_thread", "join_thread")]
    thread = state.threads[tid]
    if sess.name not in state.rooms[thread.room].member_names:
        return [_nack(sess.id, "not_in_room", "join_thread")]
    if thread.state == "closed":
        return [_nack(sess.id, "thread_closed", "join_thread")]
    if sess.name in thread.members:
        return []  # already a member; idempotent
    if thread.private:
        # Private threads: late admission is a non-goal in v1 (§1,
        # non-goal "Late admission into private threads").
        return [_nack(sess.id, "private_thread", "join_thread")]
    thread.members.append(sess.name)
    return []


def _handle_leave_thread(state: LobbyState, sess: Session, p: dict) -> list[Send]:
    tid = p.get("thread_id")
    if not isinstance(tid, str) or tid not in state.threads:
        return [_nack(sess.id, "invalid_thread", "leave_thread")]
    thread = state.threads[tid]
    if sess.name not in thread.members:
        return []  # idempotent
    was_current_speaker = (thread.current_speaker == sess.name)
    idx = thread.members.index(sess.name)
    thread.members.remove(sess.name)
    # Adjust cursor so it points at the same *next* speaker semantics.
    if not thread.members:
        thread.state = "closed"
        thread.cursor = 0
        return []
    if idx < thread.cursor:
        thread.cursor -= 1
    thread.cursor %= len(thread.members)
    # Initiator leaving: ownership transfers to the new position-0
    # member (§3, rule 6).
    if sess.name == thread.initiator:
        thread.initiator = thread.members[0]
    # If the leaver WAS the current speaker the rotation cursor now
    # points at a new member who has not been told it is their turn —
    # emit `your_turn` so the thread doesn't silently hang (H1 fix).
    out: list[Send] = []
    if was_current_speaker and thread.state == "open":
        out.extend(_emit_your_turn(state, thread))
    return out


def _handle_close_thread(state: LobbyState, sess: Session, p: dict) -> list[Send]:
    tid = p.get("thread_id")
    if not isinstance(tid, str) or tid not in state.threads:
        return [_nack(sess.id, "invalid_thread", "close_thread")]
    thread = state.threads[tid]
    if sess.name != thread.initiator:
        return [_nack(sess.id, "not_initiator", "close_thread")]
    thread.state = "closed"
    return []


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_thread(state: LobbyState, sess: Session, p: dict,
                    ref_kind: str) -> tuple[Optional[Thread], Optional[Send]]:
    tid = p.get("thread_id")
    if not isinstance(tid, str) or tid not in state.threads:
        return None, _nack(sess.id, "invalid_thread", ref_kind)
    thread = state.threads[tid]
    if thread.state == "closed":
        return None, _nack(sess.id, "thread_closed", ref_kind)
    # Everyone (clive or human) must be in the thread's room. Thread
    # membership survives session drops, but room membership is
    # session-scoped — a reconnecting clive must rejoin before acting.
    if thread.room not in sess.joined_rooms:
        return None, _nack(sess.id, "not_in_room", ref_kind)
    # Clives additionally must be in thread.members. Humans are
    # exempt here because §3.3 lets them inject into any public
    # thread they are room-members of; _handle_human_say enforces
    # the private-thread membership requirement.
    if sess.client_kind != "human" and sess.name not in thread.members:
        return None, _nack(sess.id, "not_in_thread", ref_kind)
    return thread, None


def _advance_cursor(thread: Thread) -> None:
    """Rotate cursor to the next member. Phase 2 treats all members as
    online; Phase 6 will add offline-skipping."""
    if thread.members:
        thread.cursor = (thread.cursor + 1) % len(thread.members)


def _fanout_message(state: LobbyState, thread: Thread, sender: str,
                    kind: str, body: Optional[str]) -> list[Send]:
    """Emit say/pass frames to the thread's fanout set, stamped with
    ``from: sender``. The sender is always excluded — they just typed
    the frame, echoing it back is noise.

    Recipient set (§4.3):
      - Private threads: thread members only.
      - Public threads: union of thread members and room members. A
        member who joined the thread and then dropped their SSH session
        is still on thread.members; they are naturally skipped below
        when their name has no live session.
    """
    payload: dict = {"thread_id": thread.thread_id, "from": sender}
    if kind == "say":
        payload["body"] = body
    if thread.private:
        recipient_names: set[str] = set(thread.members)
    else:
        room = state.rooms.get(thread.room)
        room_members: set[str] = set(room.member_names) if room else set()
        recipient_names = set(thread.members) | room_members
    out: list[Send] = []
    for recipient_name in recipient_names:
        if recipient_name == sender:
            continue
        session_id = state.name_to_session.get(recipient_name)
        if session_id is None:
            continue  # offline — Phase 6 adds replay-on-rejoin
        out.append(Send(session_id, kind, dict(payload)))
    return out


def _emit_your_turn(state: LobbyState, thread: Thread) -> list[Send]:
    """Send ``your_turn`` to the current speaker, carrying the last-K
    messages per §4.2. Skipped if the thread is dormant/closed."""
    if thread.state != "open":
        return []
    speaker = thread.current_speaker
    if not speaker:
        return []
    session_id = state.name_to_session.get(speaker)
    if session_id is None:
        return []  # offline — Phase 6 will auto-pass via timer
    recent = []
    for m in thread.messages[-state.recent_window:]:
        entry: dict = {"from": m.sender, "kind": m.kind}
        if m.body is not None:
            entry["body"] = m.body
        recent.append(entry)
    payload = {
        "thread_id": thread.thread_id,
        "room": thread.room,
        "name": speaker,
        "members": list(thread.members),
        "message_index": len(thread.messages),
        "recent": recent,
    }
    return [Send(session_id, "your_turn", payload)]


def _nack(session_id: int, reason: str, ref_kind: str) -> Send:
    return Send(session_id, "nack", {"reason": reason, "ref_kind": ref_kind})


def _ack_reject(session_id: int, name: str, reason: str) -> Send:
    return Send(session_id, "session_ack",
                {"name": name, "accepted": False, "reason": reason})
