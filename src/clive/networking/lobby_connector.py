"""Localhost lobby connector.

Given a locally-registered lobby name (as appears under
``~/.clive/instances/``), open a Unix-socket connection to its
broker, complete the ``NONCE`` handshake, and hand the caller a
ready-to-use blocking socket plus the session nonce.

    sock, nonce = connect_local("lobby")
    # `sock` ready for framed IO
    # `nonce` used to stamp outbound frames and decode inbound ones

For remote (SSH-reachable) lobbies the transport is a subprocess
pipe running ``clive --role lobby-client`` on the remote, with the
nonce forwarded via ``CLIVE_FRAME_NONCE`` env var. That path comes
in a later commit; this module handles the co-located case first so
the CLI wire-up (``--join room@lobby``) has a name-resolution target
on day one.

Design reference: docs/plans/2026-04-14-clive-rooms-design.md §6.3
(membership declaration) and §8.2 (clients reach the lobby).
"""
from __future__ import annotations

import socket
from pathlib import Path
from typing import Optional

from protocol import generate_nonce
import registry


class ConnectError(RuntimeError):
    """Raised when a lobby cannot be reached. Kept distinct from the
    generic OSError raised by the socket layer so callers can
    present a helpful user-facing message without swallowing real
    IO errors elsewhere."""


def connect_local(
    lobby_name: str,
    *,
    nonce: Optional[str] = None,
    registry_dir: Optional[Path] = None,
) -> tuple[socket.socket, str]:
    """Connect to a locally-registered lobby.

    Returns ``(sock, nonce)`` where ``sock`` is a connected blocking
    ``AF_UNIX`` socket that has already completed the ``NONCE``
    handshake line, and ``nonce`` is the string the caller should
    use to stamp outbound frames and decode inbound ones.

    Keeps the socket blocking so a caller using it with ``sendall``
    does not have to handle partial-write bookkeeping. Callers that
    plug the socket into a selectors loop should flip to
    non-blocking themselves (and handle the write path accordingly).

    If ``nonce`` is not supplied a fresh one is minted via
    ``protocol.generate_nonce``. For the SSH transport (later
    commit) the outer process supplies the nonce so the remote
    lobby-client bridge can read it from ``CLIVE_FRAME_NONCE``.
    """
    entry = registry.get_instance(lobby_name, registry_dir=registry_dir)
    if entry is None:
        raise ConnectError(
            f"lobby {lobby_name!r} is not registered as a live local "
            f"instance — is the broker running?"
        )
    if entry.get("role") != "broker":
        raise ConnectError(
            f"instance {lobby_name!r} is not a broker "
            f"(role={entry.get('role')!r})"
        )
    sock_path = entry.get("socket_path")
    if not sock_path:
        raise ConnectError(
            f"broker {lobby_name!r} has no socket_path in its registry "
            f"entry — schema drift or partial startup"
        )

    n = nonce if nonce is not None else generate_nonce()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
    except OSError as e:
        sock.close()
        raise ConnectError(
            f"cannot connect to lobby {lobby_name!r} at {sock_path}: {e}"
        ) from e

    try:
        sock.sendall(f"NONCE {n}\n".encode("ascii"))
    except OSError as e:
        sock.close()
        raise ConnectError(
            f"handshake with lobby {lobby_name!r} failed: {e}"
        ) from e

    return sock, n
