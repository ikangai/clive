"""Tests for ``networking/lobby_connector.py`` — the localhost
name-resolution helper that a member clive uses when it sees
``--join room@lobby``.

The connector reads the local instance registry, finds the broker's
socket path, opens a Unix socket, and completes the ``NONCE`` line
handshake. For SSH-reachable lobbies the transport flips to a
subprocess pipe (``ssh lobbyhost clive --role lobby-client``); that
comes in a later commit.

Tests use a real ``LobbyServer`` so the handshake is end-to-end
rather than mocked — the handshake is the contract.
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from protocol import decode_all, encode
from lobby_server import LobbyServer
from lobby_connector import ConnectError, connect_local


# ─── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_registry(tmp_path):
    d = tmp_path / "instances"
    d.mkdir()
    yield d


@pytest.fixture
def running_lobby(tmp_registry):
    """A real LobbyServer accepting connections on a short ephemeral
    socket path. Cleans up the socket, registry entry, and server
    thread on teardown."""
    fd, sock = tempfile.mkstemp(prefix="clv-", suffix=".sock")
    os.close(fd); os.unlink(sock)
    srv = LobbyServer(
        socket_path=sock,
        registry_dir=tmp_registry,
        instance_name="testlobby",
    )
    srv.start()
    t = threading.Thread(target=srv.run_forever, daemon=True)
    t.start()
    try:
        yield srv, sock
    finally:
        srv.shutdown()
        t.join(timeout=2.0)
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass


# ─── Happy path ─────────────────────────────────────────────────────────────


def test_connect_local_completes_handshake(running_lobby, tmp_registry):
    """`connect_local` resolves the name through the local registry
    and returns a socket that has already completed the NONCE
    handshake, so a follow-up `session_hello` succeeds."""
    _srv, _sock_path = running_lobby
    sock, nonce = connect_local("testlobby", registry_dir=tmp_registry)
    try:
        assert isinstance(nonce, str) and len(nonce) > 0
        # Drive a session_hello end-to-end: if the handshake was
        # bad, the lobby would have closed the connection and this
        # send/recv would see EOF.
        sock.sendall(
            (encode("session_hello",
                    {"client_kind": "clive", "name": "alice"},
                    nonce=nonce) + "\n").encode()
        )
        sock.settimeout(1.0)
        data = sock.recv(4096)
        frames = decode_all(data.decode("utf-8", errors="replace"),
                            nonce=nonce)
        assert frames and frames[0].kind == "session_ack"
        assert frames[0].payload["accepted"] is True
    finally:
        sock.close()


def test_explicit_nonce_is_honoured(running_lobby, tmp_registry):
    """The caller can supply the nonce (how the SSH path will work —
    the outer generates it and the subprocess reads it from env).
    Here we just verify the connector does not override it."""
    _srv, _ = running_lobby
    sock, nonce = connect_local("testlobby", nonce="fixed123",
                                registry_dir=tmp_registry)
    try:
        assert nonce == "fixed123"
        # Sanity: frames stamped with "fixed123" are accepted.
        sock.sendall(
            (encode("session_hello",
                    {"client_kind": "clive", "name": "alice"},
                    nonce="fixed123") + "\n").encode()
        )
        sock.settimeout(1.0)
        frames = decode_all(sock.recv(4096).decode(), nonce="fixed123")
        assert frames[0].kind == "session_ack"
    finally:
        sock.close()


def test_nonces_are_unique_across_calls(running_lobby, tmp_registry):
    """Without an explicit nonce, each call must mint a fresh one.
    Two concurrent members sharing a nonce would be a §7.2 violation
    (compromised member could forge for the other)."""
    _srv, _ = running_lobby
    _, n1 = connect_local("testlobby", registry_dir=tmp_registry)
    _, n2 = connect_local("testlobby", registry_dir=tmp_registry)
    assert n1 != n2


# ─── Resolution failures ────────────────────────────────────────────────────


def test_unknown_lobby_name_raises_connect_error(tmp_registry):
    with pytest.raises(ConnectError, match="not registered"):
        connect_local("no-such-lobby", registry_dir=tmp_registry)


def test_non_broker_instance_is_rejected(tmp_registry):
    """A regular named clive is not a lobby — refuse to connect.
    The registry carries a `role` field precisely so this check
    is cheap and doesn't require a round-trip."""
    import registry as _reg
    _reg.register(
        name="regular", pid=os.getpid(),
        tmux_session="", tmux_socket="", toolset="", task="",
        conversational=True, session_dir="",
        registry_dir=tmp_registry,
        # No role=broker.
    )
    with pytest.raises(ConnectError, match="not a broker"):
        connect_local("regular", registry_dir=tmp_registry)


def test_broker_without_socket_path_is_rejected(tmp_registry):
    """Defensive: a registry entry declared role=broker but missing
    socket_path cannot be connected. Would indicate a registry-schema
    drift bug elsewhere; we should surface it loudly."""
    import registry as _reg
    _reg.register(
        name="noSocket", pid=os.getpid(),
        tmux_session="", tmux_socket="", toolset="", task="",
        conversational=True, session_dir="",
        registry_dir=tmp_registry,
        role="broker",
        # No socket_path.
    )
    with pytest.raises(ConnectError, match="socket_path"):
        connect_local("noSocket", registry_dir=tmp_registry)


def test_missing_socket_file_raises_connect_error(tmp_registry):
    """If the socket file is gone (broker crashed uncleanly),
    registry pruning should eventually evict the entry; until then
    we surface the connection failure as a ConnectError rather
    than a bare OSError."""
    import registry as _reg
    _reg.register(
        name="stale", pid=os.getpid(),
        tmux_session="", tmux_socket="", toolset="", task="",
        conversational=True, session_dir="",
        registry_dir=tmp_registry,
        role="broker",
        socket_path="/tmp/definitely-not-a-real-socket-xyz.sock",
    )
    with pytest.raises(ConnectError, match="cannot connect"):
        connect_local("stale", registry_dir=tmp_registry)
