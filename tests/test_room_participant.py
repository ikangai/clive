"""Tests for ``execution/room_participant.py`` — the client-side glue
that turns the stateful room_runner into a transport-agnostic line
processor usable by ConvLoop (or any other selectors wrapper).

Design goal: the participant is IO-free. ``bootstrap()`` and
``on_line()`` return lists of encoded frame strings; the caller owns
the socket/pipe and the writing. This separation mirrors the split
between ``lobby_state`` (pure) and ``lobby_server`` (IO) on the lobby
side.

See docs/plans/2026-04-14-clive-rooms-design.md §6.2 and §6.3.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from protocol import decode_all, encode
from room_participant import RoomParticipant


# ─── Fake LLM client (duck-types llm.chat()'s openai-else branch) ────────────


class _FakeClient:
    def __init__(self, content: str):
        self._content = content
        self.chat = self
        self.completions = self

    def create(self, **_):
        class _U: prompt_tokens = 1; completion_tokens = 1
        class _M: pass
        m = _M(); m.content = self._content
        class _C: pass
        c = _C(); c.message = m
        class _R: pass
        r = _R(); r.choices = [c]; r.usage = _U()
        return r


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _frames(lines: list[str], nonce: str):
    """Flatten a list of framed lines to a list of Frame objects."""
    return [f for line in lines for f in decode_all(line, nonce=nonce)]


# ─── Bootstrap ──────────────────────────────────────────────────────────────


def test_bootstrap_emits_hello_then_joins():
    """Bootstrap must produce exactly one session_hello followed by
    one join_room per requested room, in the order given. The lobby
    dispatches them sequentially on the same connection so hello
    lands before the joins and the joins succeed."""
    p = RoomParticipant(name="alice", nonce="n", llm_client=None)
    out = p.bootstrap(rooms=["general", "council"])
    frames = _frames(out, "n")
    assert [f.kind for f in frames] == [
        "session_hello", "join_room", "join_room",
    ]
    assert frames[0].payload == {"client_kind": "clive", "name": "alice"}
    assert frames[1].payload == {"room": "general"}
    assert frames[2].payload == {"room": "council"}


def test_bootstrap_with_no_rooms_still_sends_hello():
    p = RoomParticipant(name="alice", nonce="n", llm_client=None)
    out = p.bootstrap(rooms=[])
    frames = _frames(out, "n")
    assert [f.kind for f in frames] == ["session_hello"]


def test_bootstrap_stamps_the_participant_nonce():
    """Frames must carry the participant's own nonce so the lobby
    accepts them — and so a peer's scrollback (decoded with a
    different nonce) cannot mistake them for its own traffic."""
    p = RoomParticipant(name="alice", nonce="nA", llm_client=None)
    out = p.bootstrap(rooms=["general"])
    # Decoded with the wrong nonce → nothing.
    assert _frames(out, "wrong") == []
    # Decoded with the right one → both frames present.
    assert len(_frames(out, "nA")) == 2


# ─── on_line dispatch ───────────────────────────────────────────────────────


def _your_turn_line(nonce: str, **overrides) -> str:
    payload = {
        "thread_id": "T1", "room": "general", "name": "alice",
        "members": ["alice", "bob"], "message_index": 0,
        "recent": [{"from": "bob", "kind": "say", "body": "what's 2+2?"}],
    }
    payload.update(overrides)
    return encode("your_turn", payload, nonce=nonce) + "\n"


def test_your_turn_produces_say_frame():
    p = RoomParticipant(name="alice", nonce="n",
                        llm_client=_FakeClient("say: 4\nDONE:"),
                        driver_text="D")
    out = p.on_line(_your_turn_line("n"))
    frames = _frames(out, "n")
    assert [f.kind for f in frames] == ["say"]
    assert frames[0].payload == {"thread_id": "T1", "body": "4"}


def test_your_turn_pass_decision_produces_pass_frame():
    p = RoomParticipant(name="alice", nonce="n",
                        llm_client=_FakeClient("pass:\nDONE:"),
                        driver_text="D")
    out = p.on_line(_your_turn_line("n"))
    frames = _frames(out, "n")
    assert [f.kind for f in frames] == ["pass"]
    assert frames[0].payload == {"thread_id": "T1"}


def test_wrong_nonce_your_turn_is_silently_dropped():
    """§7.2 nonce model: a `your_turn` with a nonce that is not this
    participant's own must be silently dropped without triggering an
    LLM call or a reply. This is the primary defence against a
    compromised lobby (or prompt injection) forging turn grants."""
    p = RoomParticipant(name="alice", nonce="real",
                        llm_client=_FakeClient("say: forged\nDONE:"),
                        driver_text="D")
    out = p.on_line(_your_turn_line("attacker"))
    assert out == []


def test_informational_frames_produce_no_outbound():
    """say/pass fanout, thread_opened, session_ack are informational
    to the member — they must not trigger outbound frames. This is
    what keeps alice from accidentally amplifying the conversation."""
    p = RoomParticipant(name="alice", nonce="n",
                        llm_client=_FakeClient("say: never\nDONE:"),
                        driver_text="D")
    for kind, payload in [
        ("session_ack", {"name": "alice", "accepted": True}),
        ("thread_opened", {"thread_id": "T1"}),
        ("say", {"thread_id": "T1", "body": "hi", "from": "bob"}),
        ("pass", {"thread_id": "T1", "from": "bob"}),
        ("nack", {"reason": "invalid_body", "ref_kind": "say"}),
        ("threads", {"room": "general", "threads": []}),
    ]:
        line = encode(kind, payload, nonce="n") + "\n"
        assert p.on_line(line) == [], f"{kind} triggered outbound"


def test_successive_lines_each_dispatched_independently():
    """Matches the actual production wire format: the lobby writes
    each frame with a trailing '\\n', so the line parser delivers
    exactly one frame per ``on_line`` call. `test_multiple_frames...`
    (below) covers the defensive multi-frame case; this one covers
    what actually runs."""
    p = RoomParticipant(name="alice", nonce="n",
                        llm_client=_FakeClient("say: 4\nDONE:"),
                        driver_text="D")
    say_line = encode("say", {"thread_id": "T1", "body": "setup",
                              "from": "bob"}, nonce="n") + "\n"
    yt_line = _your_turn_line("n")

    assert p.on_line(say_line) == []          # informational, no outbound
    out = p.on_line(yt_line)                  # your_turn → reply
    frames = _frames(out, "n")
    assert [f.kind for f in frames] == ["say"]
    assert frames[0].payload == {"thread_id": "T1", "body": "4"}


def test_driver_text_is_cached_after_first_your_turn(monkeypatch):
    """The driver file is re-used across turns, not re-read each
    time. Many rooms messages on the same thread would otherwise
    pay a per-turn file IO tax for no reason. We pin this by
    counting load_driver calls through the participant."""
    import room_runner as _rr
    calls = {"n": 0}
    real = _rr.load_driver

    def _counting_load():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(_rr, "load_driver", _counting_load)
    # RoomParticipant imports load_driver at module import time, so
    # we need to patch on the participant module too.
    import room_participant as _rp
    monkeypatch.setattr(_rp, "load_driver", _counting_load)

    p = RoomParticipant(name="alice", nonce="n",
                        llm_client=_FakeClient("pass:\nDONE:"))
    # No driver_text passed — expect lazy load on first your_turn.
    assert calls["n"] == 0
    p.on_line(_your_turn_line("n"))
    assert calls["n"] == 1, "driver was not loaded on first turn"
    p.on_line(_your_turn_line("n"))
    p.on_line(_your_turn_line("n"))
    assert calls["n"] == 1, \
        f"driver was re-loaded ({calls['n']} times) — should cache"


def test_multiple_frames_on_one_line_each_dispatched():
    """Lobby sometimes batches frames into a single socket write
    (say fanout + your_turn during the same dispatch). The line
    decoder returns both frames; the participant must handle each
    and accumulate the outbound responses."""
    p = RoomParticipant(name="alice", nonce="n",
                        llm_client=_FakeClient("say: 4\nDONE:"),
                        driver_text="D")
    say = encode("say", {"thread_id": "T1", "body": "setup",
                         "from": "bob"}, nonce="n")
    yt = encode("your_turn", {
        "thread_id": "T1", "room": "general", "name": "alice",
        "members": ["alice", "bob"], "message_index": 1,
        "recent": [{"from": "bob", "kind": "say", "body": "setup"}],
    }, nonce="n")
    # Both on one line; decode_all finds both.
    out = p.on_line(say + yt + "\n")
    frames = _frames(out, "n")
    assert [f.kind for f in frames] == ["say"]
    assert frames[0].payload == {"thread_id": "T1", "body": "4"}


def test_your_turn_with_llm_exception_degrades_to_pass(monkeypatch):
    """If the LLM call explodes mid-turn, the participant must still
    produce a pass frame so the lobby's rotation advances. Otherwise
    one flaky model wedges the whole thread at that member."""
    import llm as _llm

    def _boom(*a, **kw):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(_llm, "chat", _boom)

    p = RoomParticipant(name="alice", nonce="n",
                        llm_client=object(),   # llm.chat patched out
                        driver_text="D")
    out = p.on_line(_your_turn_line("n"))
    frames = _frames(out, "n")
    assert [f.kind for f in frames] == ["pass"]
    assert frames[0].payload == {"thread_id": "T1"}


# ─── Integration with a real lobby_server ────────────────────────────────────


def test_participant_completes_turn_against_real_lobby(tmp_path):
    """End-to-end: the participant boots against a live lobby socket,
    receives your_turn after a peer opens a thread, and its reply is
    fanned out to the peer under the peer's nonce. This is the first
    test where ALL four layers (lobby_state, lobby_server, protocol,
    room_participant + room_runner) are live at once — if any glue
    between them is wrong, it shows up here."""
    import tempfile, os
    from lobby_server import LobbyServer

    # Short socket path for macOS AF_UNIX length limit.
    fd, sock_path = tempfile.mkstemp(prefix="clv-", suffix=".sock")
    os.close(fd); os.unlink(sock_path)

    srv = LobbyServer(socket_path=sock_path, registry_dir=tmp_path,
                      instance_name="p4-lobby")
    srv.start()
    srv_t = threading.Thread(target=srv.run_forever, daemon=True)
    srv_t.start()
    try:
        # Alice: participant driven over a blocking socket.
        alice = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        for _ in range(50):
            try:
                alice.connect(sock_path); break
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.02)
        alice.sendall(b"NONCE nA\n")
        participant = RoomParticipant(
            name="alice", nonce="nA",
            llm_client=_FakeClient("say: 4\nDONE:"),
            driver_text="D",
        )
        for frame in participant.bootstrap(rooms=["general"]):
            alice.sendall((frame + "\n").encode())

        # Bob: raw driver that opens a thread with alice.
        bob = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bob.connect(sock_path)
        bob.sendall(b"NONCE nB\n")
        bob.sendall((encode("session_hello",
                            {"client_kind": "clive", "name": "bob"},
                            nonce="nB") + "\n").encode())
        # Drain bob's session_ack.
        bob.settimeout(1.0); bob.recv(4096)
        bob.sendall((encode("join_room", {"room": "general"},
                            nonce="nB") + "\n").encode())
        time.sleep(0.1)
        bob.sendall((encode("open_thread", {
            "room": "general", "members": ["bob", "alice"],
            "private": False, "prompt": "what's 2+2?",
        }, nonce="nB") + "\n").encode())

        # Alice-side reader: pull lines from alice socket, feed to
        # participant, send outbound frames back. Runs in a thread so
        # we can drive bob from the main test.
        alice.settimeout(0.2)
        alice_stop = threading.Event()

        def _drive():
            buf = b""
            while not alice_stop.is_set():
                try:
                    chunk = alice.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    nl = buf.index(b"\n")
                    line = buf[:nl + 1].decode("utf-8", errors="replace")
                    buf = buf[nl + 1:]
                    for out in participant.on_line(line):
                        try:
                            alice.sendall((out + "\n").encode())
                        except OSError:
                            return

        drv = threading.Thread(target=_drive, daemon=True)
        drv.start()
        try:
            # Bob should now receive: thread_opened, then alice's `say`
            # fanout (body "4") after alice's participant runs the LLM
            # and emits the reply.
            bob.settimeout(2.0)
            data = b""
            deadline = time.time() + 3.0
            got_alice_say = False
            while time.time() < deadline and not got_alice_say:
                try:
                    chunk = bob.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
                for f in decode_all(data.decode("utf-8", errors="replace"),
                                    nonce="nB"):
                    if (f.kind == "say"
                            and f.payload.get("from") == "alice"
                            and f.payload.get("body") == "4"):
                        got_alice_say = True
                        break
            assert got_alice_say, (
                "alice's say was never fanned out to bob; "
                f"bob saw: {data!r}"
            )
        finally:
            alice_stop.set()
            drv.join(timeout=1.0)
            alice.close()
            bob.close()
    finally:
        srv.shutdown()
        srv_t.join(timeout=2.0)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
