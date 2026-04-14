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
