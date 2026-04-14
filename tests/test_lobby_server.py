"""Integration tests for the lobby IO layer (Phase 2).

These tests spin up the selectors-based lobby server on an ephemeral
Unix socket, connect raw clients, and drive the session-layer
handshake and a few of the simpler dispatch paths end-to-end. Rooms
and threads themselves live in the pure state machine and are covered
by ``test_lobby_state.py``; here we validate only what the IO layer
adds: socket accept, per-connection nonce handshake, framed IO, frame
dispatch, and clean session drop.

See docs/plans/2026-04-14-clive-rooms-design.md §5 (lobby
implementation) and §11 Phase 2.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from protocol import Frame, decode_all, encode
from lobby_server import LobbyServer


# ─── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_socket():
    # macOS caps AF_UNIX paths near 104 chars, shorter than pytest's
    # default tmp_path. Use a short name under the system tmpdir.
    fd, p = tempfile.mkstemp(prefix="clv-", suffix=".sock")
    os.close(fd)
    os.unlink(p)   # server will create it
    try:
        yield p
    finally:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


@pytest.fixture
def tmp_registry(tmp_path: Path):
    d = tmp_path / "instances"
    d.mkdir()
    yield d


@pytest.fixture
def server(tmp_socket, tmp_registry):
    """A started-but-not-running server. Tests call ``start_thread()``."""
    srv = LobbyServer(
        socket_path=tmp_socket,
        registry_dir=tmp_registry,
        instance_name="test-lobby",
    )
    srv.start()
    t = threading.Thread(target=srv.run_forever, name="lobby", daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    t.join(timeout=2.0)


def _connect(socket_path: str, nonce: str) -> socket.socket:
    """Open a Unix socket and complete the nonce handshake. Returns the
    connected socket ready for framed IO."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # The server socket is created inside the fixture's thread; retry
    # briefly if it hasn't appeared yet.
    for _ in range(50):
        try:
            s.connect(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.02)
    else:
        raise RuntimeError(f"lobby socket never appeared at {socket_path}")
    s.sendall(f"NONCE {nonce}\n".encode())
    return s


def _recv_frames(s: socket.socket, nonce: str, *, timeout: float = 1.0) -> list[Frame]:
    """Read one chunk from the socket and decode frames."""
    s.settimeout(timeout)
    data = b""
    # Read until we see at least one terminator or timeout.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
        if b">>>" in data:
            # Likely got a full frame; keep reading briefly for trailing ones.
            s.settimeout(0.05)
            try:
                while True:
                    more = s.recv(4096)
                    if not more:
                        break
                    data += more
            except socket.timeout:
                pass
            break
    return decode_all(data.decode("utf-8", errors="replace"), nonce=nonce)


# ─── Socket lifecycle / registry ─────────────────────────────────────────────


def test_server_creates_socket_and_registry_entry(server, tmp_socket, tmp_registry):
    # Socket exists and is a socket.
    assert Path(tmp_socket).exists()
    assert Path(tmp_socket).is_socket()
    # Registry entry exists with role=broker.
    entries = list(tmp_registry.glob("*.json"))
    assert len(entries) == 1
    data = json.loads(entries[0].read_text())
    assert data["name"] == "test-lobby"
    assert data["role"] == "broker"


def test_second_broker_with_same_name_is_rejected(server, tmp_registry):
    """A second LobbyServer.start() under the same instance name must
    refuse to run — otherwise it would unlink the first broker's
    socket and overwrite its registry entry, silently killing
    connectivity for every already-connected client."""
    fd, p2 = tempfile.mkstemp(prefix="clv-", suffix=".sock")
    os.close(fd)
    os.unlink(p2)
    try:
        srv2 = LobbyServer(
            socket_path=p2,
            registry_dir=tmp_registry,
            instance_name="test-lobby",   # same as `server` fixture
        )
        with pytest.raises(RuntimeError, match="already running"):
            srv2.start()
        # The second server must NOT have created its socket.
        assert not Path(p2).exists()
    finally:
        try:
            os.unlink(p2)
        except FileNotFoundError:
            pass


def test_socket_is_owner_only(server, tmp_socket):
    """The socket file must be 0o600 — world-readable would let any
    local user connect and speak the lobby protocol, bypassing the
    SSH auth gate. The umask-tightening + chmod defence-in-depth
    together should guarantee the mode."""
    import stat
    mode = stat.S_IMODE(os.stat(tmp_socket).st_mode)
    assert mode == 0o600, f"socket mode is {oct(mode)}, want 0o600"


def test_server_removes_socket_on_shutdown(tmp_socket, tmp_registry):
    srv = LobbyServer(socket_path=tmp_socket, registry_dir=tmp_registry,
                      instance_name="shutdown-lobby")
    srv.start()
    t = threading.Thread(target=srv.run_forever, daemon=True)
    t.start()
    assert Path(tmp_socket).exists()
    srv.shutdown()
    t.join(timeout=2.0)
    assert not Path(tmp_socket).exists()
    # Registry deregistered too.
    assert not (tmp_registry / "shutdown-lobby.json").exists()


# ─── Handshake ───────────────────────────────────────────────────────────────


def test_session_hello_accepted(server, tmp_socket):
    s = _connect(tmp_socket, nonce="n1")
    s.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="n1") + "\n").encode())
    frames = _recv_frames(s, nonce="n1")
    assert len(frames) == 1
    assert frames[0].kind == "session_ack"
    assert frames[0].payload == {"name": "alice", "accepted": True}
    s.close()


def test_session_hello_wrong_nonce_is_dropped(server, tmp_socket):
    """Frame with a nonce that does not match the session's declared
    nonce must be dropped silently (no ack, no nack). The client should
    see no response until it sends a correctly-nonced frame."""
    s = _connect(tmp_socket, nonce="real")
    # Send a session_hello stamped with the wrong nonce.
    s.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="wrong") + "\n").encode())
    # Nothing should come back.
    assert _recv_frames(s, nonce="real", timeout=0.3) == []
    # Now send a correctly-nonced hello — it should succeed.
    s.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="real") + "\n").encode())
    frames = _recv_frames(s, nonce="real")
    assert frames[0].kind == "session_ack"
    assert frames[0].payload["accepted"] is True
    s.close()


def test_name_in_use_rejection(server, tmp_socket):
    a = _connect(tmp_socket, nonce="nA")
    a.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="nA") + "\n").encode())
    assert _recv_frames(a, nonce="nA")[0].payload["accepted"] is True

    b = _connect(tmp_socket, nonce="nB")
    b.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="nB") + "\n").encode())
    frames = _recv_frames(b, nonce="nB")
    assert frames[0].kind == "session_ack"
    assert frames[0].payload["accepted"] is False
    assert frames[0].payload["reason"] == "name_in_use"
    a.close()
    b.close()


def test_name_freed_on_disconnect(server, tmp_socket):
    a = _connect(tmp_socket, nonce="nA")
    a.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="nA") + "\n").encode())
    assert _recv_frames(a, nonce="nA")[0].payload["accepted"] is True
    a.close()
    # Give the server a moment to detect the close.
    time.sleep(0.1)
    b = _connect(tmp_socket, nonce="nB")
    b.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="nB") + "\n").encode())
    frames = _recv_frames(b, nonce="nB")
    assert frames[0].payload["accepted"] is True
    b.close()


def test_pre_hello_frame_is_nacked(server, tmp_socket):
    """Any non-hello frame before session_hello returns
    session_hello_required per lobby_state dispatch."""
    s = _connect(tmp_socket, nonce="n")
    s.sendall((encode("join_room", {"room": "general"}, nonce="n") + "\n").encode())
    frames = _recv_frames(s, nonce="n")
    assert frames[0].kind == "nack"
    assert frames[0].payload == {
        "reason": "session_hello_required", "ref_kind": "join_room",
    }
    s.close()


def test_alive_frames_are_silently_accepted(server, tmp_socket):
    """Lobby must not nack `alive` frames — they are the keepalive
    ticker emission and should be absorbed without response."""
    s = _connect(tmp_socket, nonce="n")
    s.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="n") + "\n").encode())
    assert _recv_frames(s, nonce="n")[0].kind == "session_ack"
    s.sendall((encode("alive", {"ts": 1.0}, nonce="n") + "\n").encode())
    assert _recv_frames(s, nonce="n", timeout=0.3) == []
    s.close()


def test_unknown_kind_is_nacked(server, tmp_socket):
    """A valid-format frame whose kind is not handled by the lobby
    (e.g. `context`, which is meaningful inside a clive pane but not
    to the lobby) is nacked with reason=unknown_kind."""
    s = _connect(tmp_socket, nonce="n")
    s.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="n") + "\n").encode())
    assert _recv_frames(s, nonce="n")[0].kind == "session_ack"
    s.sendall((encode("context", {"foo": "bar"}, nonce="n") + "\n").encode())
    frames = _recv_frames(s, nonce="n")
    assert frames[0].kind == "nack"
    assert frames[0].payload["reason"] == "unknown_kind"
    assert frames[0].payload["ref_kind"] == "context"
    s.close()


# ─── Multiple concurrent connections ─────────────────────────────────────────


def test_two_sessions_get_independent_nonces(server, tmp_socket):
    """Each session has its own nonce. A fanout-style send from the
    server must reach the intended recipient under that recipient's
    nonce. We can't test fanout without rooms (Phase 3), so here we
    just verify two sessions don't bleed frames into each other."""
    a = _connect(tmp_socket, nonce="nA")
    b = _connect(tmp_socket, nonce="nB")
    a.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="nA") + "\n").encode())
    b.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "bob"},
                      nonce="nB") + "\n").encode())
    af = _recv_frames(a, nonce="nA")
    bf = _recv_frames(b, nonce="nB")
    assert af[0].payload == {"name": "alice", "accepted": True}
    assert bf[0].payload == {"name": "bob", "accepted": True}
    # Cross-decode: B's reply cannot be decoded with A's nonce.
    assert _recv_frames(a, nonce="nB", timeout=0.1) == []
    a.close()
    b.close()


def test_lobby_client_wrapper_round_trip(server, tmp_socket):
    """End-to-end: the lobby_client wrapper (SSH-invoked bridge) pipes
    a local stdin/stdout pair through to the server and back. Proves
    the wire format (NONCE line + framed lines) is consistent between
    server and client."""
    from lobby_client import run as _run

    # We drive the wrapper on two OS pipes.
    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()

    def _client():
        # Stand in for stdin/stdout.buffer: raw fd files.
        with os.fdopen(stdin_r, "rb", buffering=0) as fin, \
             os.fdopen(stdout_w, "wb", buffering=0) as fout:
            _run(socket_path=tmp_socket, nonce="nc", stdin=fin, stdout=fout)

    t = threading.Thread(target=_client, daemon=True)
    t.start()

    try:
        os.write(stdin_w, (encode("session_hello",
                                  {"client_kind": "clive", "name": "alice"},
                                  nonce="nc") + "\n").encode())
        # Read reply from the wrapper's stdout end.
        deadline = time.time() + 2.0
        buf = b""
        while time.time() < deadline:
            try:
                chunk = os.read(stdout_r, 4096)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            if not chunk:
                break
            buf += chunk
            if b">>>" in buf:
                break
        frames = decode_all(buf.decode("utf-8", errors="replace"), nonce="nc")
        assert frames and frames[0].kind == "session_ack"
        assert frames[0].payload["accepted"] is True
    finally:
        os.close(stdin_w)
        # Drain any remaining wrapper output so the thread can exit.
        try:
            os.close(stdout_r)
        except OSError:
            pass
        t.join(timeout=2.0)


# ─── End-to-end rooms flow over the wire (Phase 3 verification) ─────────────


def _hello(s: socket.socket, nonce: str, name: str, kind: str = "clive") -> None:
    s.sendall((encode("session_hello", {"client_kind": kind, "name": name},
                      nonce=nonce) + "\n").encode())


def _send(s: socket.socket, nonce: str, kind: str, payload: dict) -> None:
    s.sendall((encode(kind, payload, nonce=nonce) + "\n").encode())


def _drain(s: socket.socket, nonce: str, want_kinds: set[str],
           *, timeout: float = 1.0) -> list[Frame]:
    """Read from `s` until all frame kinds in `want_kinds` have been
    seen at least once, or the timeout expires. Returns all frames
    seen. Fails loudly on timeout so the assertion message pinpoints
    which kind was missing."""
    s.settimeout(timeout)
    data = b""
    seen: set[str] = set()
    deadline = time.time() + timeout
    while want_kinds - seen and time.time() < deadline:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
        frames = decode_all(data.decode("utf-8", errors="replace"), nonce=nonce)
        seen = {f.kind for f in frames}
    frames = decode_all(data.decode("utf-8", errors="replace"), nonce=nonce)
    missing = want_kinds - {f.kind for f in frames}
    assert not missing, (
        f"timeout waiting for kinds {sorted(missing)}; "
        f"got {[(f.kind, f.payload) for f in frames]}"
    )
    return frames


def test_end_to_end_thread_rotation(server, tmp_socket):
    """Two clives open a thread in `general`, exchange one say and one
    pass, then close. Exercises the full lobby_server + lobby_state
    composition: socket IO, per-session nonces, room membership,
    thread opening with prompt, fanout to room observers, your_turn
    routing, pass rotation, close_thread authorization.

    This is the first test that proves phase 2 IO correctly wires up
    phase 2's pure state machine — the state-machine tests can't see
    nonce stamping or socket routing; the protocol tests can't see
    rotation. This one does."""
    a = _connect(tmp_socket, nonce="nA")
    b = _connect(tmp_socket, nonce="nB")

    _hello(a, "nA", "alice")
    _hello(b, "nB", "bob")
    assert _drain(a, "nA", {"session_ack"})[0].payload["accepted"] is True
    assert _drain(b, "nB", {"session_ack"})[0].payload["accepted"] is True

    _send(a, "nA", "join_room", {"room": "general"})
    _send(b, "nB", "join_room", {"room": "general"})
    # join_room is silent on success — no frames to drain. Give the
    # server a moment to process so membership is in place before
    # alice opens the thread.
    time.sleep(0.05)

    # Alice opens a thread with both members and a prompt. State
    # machine treats the prompt as alice's first `say`, fans it out,
    # advances cursor to bob, and sends your_turn to bob.
    _send(a, "nA", "open_thread", {
        "room": "general",
        "members": ["alice", "bob"],
        "private": False,
        "prompt": "what's 2+2?",
    })

    # Alice should see: thread_opened (confirming creation). The `say`
    # fanout goes to bob (and to any room observer that isn't the
    # sender) but NOT back to alice — fanout skips sender. Alice also
    # does NOT get your_turn here because cursor advanced to bob.
    a_frames = _drain(a, "nA", {"thread_opened"})
    opened = next(f for f in a_frames if f.kind == "thread_opened")
    thread_id = opened.payload["thread_id"]
    assert thread_id.startswith("general-t")

    # Bob should see: the say fanout from alice (with the prompt) AND
    # his own your_turn.
    b_frames = _drain(b, "nB", {"say", "your_turn"})
    say_fanout = next(f for f in b_frames if f.kind == "say")
    assert say_fanout.payload["body"] == "what's 2+2?"
    assert say_fanout.payload["from"] == "alice"
    assert say_fanout.payload["thread_id"] == thread_id
    your_turn = next(f for f in b_frames if f.kind == "your_turn")
    assert your_turn.payload["thread_id"] == thread_id
    assert your_turn.payload["name"] == "bob"
    assert your_turn.payload["members"] == ["alice", "bob"]
    # `recent` must include the opening prompt (structured context per §4.2).
    recent = your_turn.payload["recent"]
    assert any(m.get("body") == "what's 2+2?" for m in recent)

    # Bob says "4". State machine fans out to alice (and room
    # observers), advances cursor back to alice, sends your_turn.
    _send(b, "nB", "say", {"thread_id": thread_id, "body": "4"})
    a_frames2 = _drain(a, "nA", {"say", "your_turn"})
    say_from_bob = next(f for f in a_frames2 if f.kind == "say")
    assert say_from_bob.payload["from"] == "bob"
    assert say_from_bob.payload["body"] == "4"

    # Alice passes. State machine fans out pass to bob and advances
    # cursor back to bob with another your_turn.
    _send(a, "nA", "pass", {"thread_id": thread_id})
    b_frames2 = _drain(b, "nB", {"pass", "your_turn"})
    pass_frame = next(f for f in b_frames2 if f.kind == "pass")
    assert pass_frame.payload["from"] == "alice"
    assert pass_frame.payload["thread_id"] == thread_id

    # Alice closes the thread (she is the initiator, so authorized).
    _send(a, "nA", "close_thread", {"thread_id": thread_id,
                                    "summary": "done"})
    # close_thread has no explicit ack in v1; the thread just transitions
    # to closed in state. Verify no nack came back.
    time.sleep(0.1)
    a.settimeout(0.1)
    try:
        leftover = a.recv(4096)
    except socket.timeout:
        leftover = b""
    frames = decode_all(leftover.decode("utf-8", errors="replace"), nonce="nA")
    nacks = [f for f in frames if f.kind == "nack"]
    assert not nacks, f"unexpected nacks: {[f.payload for f in nacks]}"

    a.close()
    b.close()


def test_non_initiator_cannot_close_thread(server, tmp_socket):
    """Authorization test that is cheapest to drive end-to-end rather
    than at the state-machine level, because it exercises nonce
    stamping on the nack coming back on bob's connection under his
    own nonce."""
    a = _connect(tmp_socket, nonce="nA")
    b = _connect(tmp_socket, nonce="nB")
    _hello(a, "nA", "alice")
    _hello(b, "nB", "bob")
    _drain(a, "nA", {"session_ack"})
    _drain(b, "nB", {"session_ack"})
    _send(a, "nA", "join_room", {"room": "general"})
    _send(b, "nB", "join_room", {"room": "general"})
    time.sleep(0.05)
    _send(a, "nA", "open_thread", {
        "room": "general",
        "members": ["alice", "bob"],
        "private": False,
        "prompt": "hi",
    })
    a_frames = _drain(a, "nA", {"thread_opened"})
    thread_id = next(f for f in a_frames
                     if f.kind == "thread_opened").payload["thread_id"]
    # Drain bob's your_turn so we start from a clean line.
    _drain(b, "nB", {"your_turn"})

    # Bob (non-initiator) tries to close. Must be nacked back on
    # bob's own socket, under bob's nonce.
    _send(b, "nB", "close_thread", {"thread_id": thread_id})
    b_frames = _drain(b, "nB", {"nack"})
    nack = next(f for f in b_frames if f.kind == "nack")
    assert nack.payload["ref_kind"] == "close_thread"
    assert "not_initiator" in nack.payload["reason"]
    a.close()
    b.close()


def test_missing_nonce_line_closes_connection(server, tmp_socket):
    """Clients that never send the NONCE handshake line must not
    starve the server. Sending raw frames without a handshake is a
    protocol violation; the server closes the socket promptly."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(tmp_socket)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.02)
    # Send a frame with no prior NONCE line.
    s.sendall((encode("session_hello",
                      {"client_kind": "clive", "name": "alice"},
                      nonce="") + "\n").encode())
    # Server must close the socket; recv() should return b"" quickly.
    # If the earlier assertion had only checked "no session_ack" we
    # would have passed trivially even for a hanging server — this
    # version pins down both the close AND the latency.
    s.settimeout(1.0)
    start = time.time()
    try:
        data = s.recv(4096)
    except socket.timeout:
        data = b"__TIMEOUT__"
    elapsed = time.time() - start
    s.close()
    assert data == b"", f"server did not close, received: {data!r}"
    assert elapsed < 0.9, f"close took too long: {elapsed:.3f}s"
