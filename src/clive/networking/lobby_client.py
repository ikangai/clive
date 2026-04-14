"""Lobby client wrapper — bridges stdin/stdout to the lobby Unix socket.

Invoked as the SSH remote command::

    ssh lobbyhost clive --role lobby-client

This process:

1. Reads the session nonce from ``CLIVE_FRAME_NONCE`` (forwarded via
   SSH ``SendEnv``), or generates a no-op empty nonce for dev use.
2. Connects to the lobby Unix socket (default ``~/.clive/lobby/lobby.sock``).
3. Writes the handshake line ``NONCE <nonce>\\n``.
4. Enters a ``selectors``-based bidi pipe: stdin → socket, socket → stdout.

Exits on EOF from either side or on socket close.

The wrapper does not parse frames; it is transparent. Framing,
encoding, and decoding live in ``protocol.py`` on both sides of the
pipe (the user's local clive on one end, the lobby server on the
other).
"""
from __future__ import annotations

import os
import selectors
import socket
import sys
from pathlib import Path
from typing import Optional


DEFAULT_SOCKET = Path.home() / ".clive" / "lobby" / "lobby.sock"


def run(socket_path: Optional[str] = None,
        nonce: Optional[str] = None,
        stdin=None, stdout=None) -> int:
    """Bridge loop. Returns an exit status suitable for ``SystemExit``."""
    sp = socket_path or str(DEFAULT_SOCKET)
    n = nonce if nonce is not None else os.environ.get("CLIVE_FRAME_NONCE", "")
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sp)
    except OSError as e:
        print(f"lobby-client: cannot connect to {sp}: {e}", file=sys.stderr)
        return 1

    try:
        sock.sendall(f"NONCE {n}\n".encode("ascii"))
    except OSError as e:
        print(f"lobby-client: handshake failed: {e}", file=sys.stderr)
        sock.close()
        return 1

    sel = selectors.DefaultSelector()
    stdin_fd = stdin.fileno()
    sock.setblocking(False)
    os.set_blocking(stdin_fd, False)

    sel.register(stdin_fd, selectors.EVENT_READ, data="stdin")
    sel.register(sock, selectors.EVENT_READ, data="sock")

    try:
        while True:
            for key, _ in sel.select(timeout=None):
                if key.data == "stdin":
                    try:
                        chunk = os.read(stdin_fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        # local EOF → tell server we're done and keep
                        # draining the socket until it closes
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        sel.unregister(stdin_fd)
                    else:
                        try:
                            sock.sendall(chunk)
                        except OSError:
                            return 0
                elif key.data == "sock":
                    try:
                        chunk = sock.recv(4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        return 0
                    try:
                        stdout.write(chunk)
                        stdout.flush()
                    except OSError:
                        return 0
    finally:
        try:
            sock.close()
        except OSError:
            pass
        sel.close()
