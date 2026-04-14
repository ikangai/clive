"""Lobby IO layer — selectors-based Unix-socket server.

This is the thin transport wrapper around ``lobby_state.handle``. The
state machine does not know about sockets, nonces, or byte buffers;
this module is what turns raw stream bytes into ``Frame`` dispatches
and routes ``Send`` responses back to the right connection, stamped
with that connection's own nonce.

Architecture (see design §5.1):

    SSH client ───► clive --role lobby-client (wrapper)
                              │
                         Unix socket
                              │
                              ▼
                     LobbyServer.run_forever
                     (single-threaded selectors loop)
                              │
                              ▼
                     lobby_state.handle()

Per-connection handshake (one line, before framed IO begins):

    NONCE <urlsafe-b64-nonce>\\n

The server captures the nonce and uses it to (a) validate inbound
frames and (b) stamp outbound frames. Per §7.2, each SSH session has
its own nonce so a compromised member cannot forge fanout frames for
another member — the other member's incoming stream is decoded with
*its own* nonce, which the attacker does not hold.

This module is deliberately minimal for Phase 2 of
docs/plans/2026-04-14-clive-rooms-design.md:
session_hello/ack + accept + nack of unknown kinds + clean drop.
Rooms/threads/fanout all flow through `handle()` and are exercised
in later phases without needing changes here.
"""
from __future__ import annotations

import logging
import os
import selectors
import socket
import threading
from pathlib import Path
from typing import Optional

from protocol import decode_all, encode
from lobby_state import LobbyState, handle
import registry

_log = logging.getLogger(__name__)

_HANDSHAKE_PREFIX = b"NONCE "
_HANDSHAKE_MAX_LEN = 256  # refuse pathologically long handshake lines


class _Conn:
    """One accepted Unix-socket connection.

    The connection goes through exactly two states:

    1. ``handshake``: we are reading the ``NONCE <value>\\n`` line.
       Any bytes that do not look like the handshake line close the
       connection.
    2. ``framed``: we read ``\\n``-delimited frame text lines and feed
       each line to ``decode_all(line, nonce=self.nonce)``. Mismatched
       frames are silently dropped by ``decode_all``.
    """

    __slots__ = ("fd", "sock", "in_buf", "out_buf", "nonce",
                 "handshake_done", "registered", "closed")

    def __init__(self, fd: int, sock: socket.socket):
        self.fd = fd
        self.sock = sock
        self.in_buf = b""
        self.out_buf = b""
        self.nonce = ""
        self.handshake_done = False
        self.registered = False
        self.closed = False


class LobbyServer:
    """Selectors-based Unix socket lobby.

    Public API:
        srv = LobbyServer(socket_path, registry_dir, instance_name)
        srv.start()               # create socket & registry entry
        srv.run_forever()         # blocks; call in a thread or main
        srv.shutdown()            # thread-safe; wakes the loop

    The server is single-threaded (one ``DefaultSelector``) per §5.1.
    Shutdown uses a self-pipe to wake the ``select()`` call from any
    thread without racing against epoll state.
    """

    def __init__(self, socket_path: str,
                 registry_dir: Optional[Path] = None,
                 instance_name: Optional[str] = None):
        self.socket_path = socket_path
        self.registry_dir = registry_dir
        self.instance_name = instance_name
        self.state = LobbyState()
        self.sel = selectors.DefaultSelector()
        self.conns: dict[int, _Conn] = {}
        self._listener: Optional[socket.socket] = None
        self._wake_r: Optional[int] = None
        self._wake_w: Optional[int] = None
        self._shutdown = threading.Event()
        self._started = False

    # ─── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Bind the socket, register in the instance registry, and
        prepare the selector. Does NOT enter the event loop."""
        if self._started:
            raise RuntimeError("LobbyServer.start() called twice")
        self._started = True

        # Remove stale socket (prior crash) before bind. This is safe
        # because the registry check at `_register_instance` rejects
        # double-start for live PIDs.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.setblocking(False)
        listener.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)   # owner-only
        listener.listen(16)
        self._listener = listener
        self.sel.register(listener, selectors.EVENT_READ, data=("listener", None))

        # Self-pipe for wakeup. A write to _wake_w makes select() return.
        r, w = os.pipe()
        os.set_blocking(r, False)
        self._wake_r, self._wake_w = r, w
        self.sel.register(r, selectors.EVENT_READ, data=("wake", None))

        self._register_instance()

    def run_forever(self) -> None:
        """Run the event loop until ``shutdown()`` is called."""
        try:
            while not self._shutdown.is_set():
                events = self.sel.select(timeout=1.0)
                for key, mask in events:
                    tag, _ = key.data
                    if tag == "listener":
                        self._accept()
                    elif tag == "wake":
                        os.read(self._wake_r, 4096)   # drain
                    elif tag == "conn":
                        conn = self.conns.get(key.fd)
                        if conn is None:
                            continue
                        if mask & selectors.EVENT_READ:
                            self._on_readable(conn)
                        if (not conn.closed) and (mask & selectors.EVENT_WRITE):
                            self._on_writable(conn)
        finally:
            self._teardown()

    def shutdown(self) -> None:
        """Signal the event loop to exit. Safe to call from any thread."""
        self._shutdown.set()
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    # ─── Accept / close ─────────────────────────────────────────────

    def _accept(self) -> None:
        assert self._listener is not None
        try:
            sock, _ = self._listener.accept()
        except BlockingIOError:
            return
        sock.setblocking(False)
        fd = sock.fileno()
        conn = _Conn(fd=fd, sock=sock)
        self.conns[fd] = conn
        self.sel.register(sock, selectors.EVENT_READ, data=("conn", conn))
        _log.debug("lobby: accepted connection fd=%d", fd)

    def _close(self, conn: _Conn) -> None:
        if conn.closed:
            return
        conn.closed = True
        if conn.registered:
            self.state.drop_session(conn.fd)
        try:
            self.sel.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        try:
            conn.sock.close()
        except OSError:
            pass
        self.conns.pop(conn.fd, None)
        _log.debug("lobby: closed connection fd=%d name=%r", conn.fd, conn.nonce)

    # ─── Read path ──────────────────────────────────────────────────

    def _on_readable(self, conn: _Conn) -> None:
        try:
            chunk = conn.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close(conn)
            return
        if not chunk:
            self._close(conn)
            return
        conn.in_buf += chunk
        if not conn.handshake_done:
            if not self._try_handshake(conn):
                return
        # After handshake (or freshly completed handshake), drain any
        # complete frame lines.
        self._drain_frames(conn)

    def _try_handshake(self, conn: _Conn) -> bool:
        """Try to consume the ``NONCE <value>\\n`` handshake line from
        ``conn.in_buf``. Returns True if handshake completed (caller
        should then drain framed bytes), False if still waiting or if
        the connection was closed on malformed input.
        """
        nl = conn.in_buf.find(b"\n")
        if nl < 0:
            if len(conn.in_buf) > _HANDSHAKE_MAX_LEN:
                _log.debug("lobby: handshake line too long on fd=%d", conn.fd)
                self._close(conn)
            return False
        line, conn.in_buf = conn.in_buf[:nl], conn.in_buf[nl + 1:]
        if not line.startswith(_HANDSHAKE_PREFIX):
            _log.debug("lobby: missing NONCE handshake on fd=%d", conn.fd)
            self._close(conn)
            return False
        nonce = line[len(_HANDSHAKE_PREFIX):].decode("ascii", errors="replace").strip()
        # Empty nonce is permitted (matches protocol.py convention for
        # dev/test paths). Validate alphabet to be safe.
        if not all(c.isalnum() or c in "_-" for c in nonce):
            _log.debug("lobby: invalid nonce charset on fd=%d", conn.fd)
            self._close(conn)
            return False
        conn.nonce = nonce
        conn.handshake_done = True
        self.state.register_session(conn.fd)
        conn.registered = True
        return True

    def _drain_frames(self, conn: _Conn) -> None:
        """Split ``conn.in_buf`` on newlines and dispatch each complete
        line as a frame. Non-frame content on a line is ignored by
        ``decode_all``."""
        while True:
            nl = conn.in_buf.find(b"\n")
            if nl < 0:
                break
            line, conn.in_buf = conn.in_buf[:nl], conn.in_buf[nl + 1:]
            if not line:
                continue
            text = line.decode("utf-8", errors="replace")
            frames = decode_all(text, nonce=conn.nonce)
            for frame in frames:
                self._dispatch(conn, frame)
                if conn.closed:
                    return

    def _dispatch(self, conn: _Conn, frame) -> None:
        import time as _time
        sends = handle(self.state, conn.fd, frame, now=_time.time())
        for send in sends:
            self._enqueue(send)

    # ─── Write path ─────────────────────────────────────────────────

    def _enqueue(self, send) -> None:
        target = self.conns.get(send.session_id)
        if target is None or target.closed:
            return
        try:
            frame = encode(send.kind, send.payload, nonce=target.nonce)
        except (ValueError, TypeError) as e:
            _log.error("lobby: failed to encode %s for fd=%d: %s",
                       send.kind, send.session_id, e)
            return
        target.out_buf += (frame + "\n").encode("utf-8")
        self._arm_write(target)

    def _arm_write(self, conn: _Conn) -> None:
        if conn.closed or not conn.out_buf:
            return
        try:
            self.sel.modify(conn.sock,
                            selectors.EVENT_READ | selectors.EVENT_WRITE,
                            data=("conn", conn))
        except (KeyError, ValueError):
            pass

    def _disarm_write(self, conn: _Conn) -> None:
        if conn.closed:
            return
        try:
            self.sel.modify(conn.sock, selectors.EVENT_READ,
                            data=("conn", conn))
        except (KeyError, ValueError):
            pass

    def _on_writable(self, conn: _Conn) -> None:
        if not conn.out_buf:
            self._disarm_write(conn)
            return
        try:
            n = conn.sock.send(conn.out_buf)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close(conn)
            return
        conn.out_buf = conn.out_buf[n:]
        if not conn.out_buf:
            self._disarm_write(conn)

    # ─── Registry & teardown ────────────────────────────────────────

    def _register_instance(self) -> None:
        if not self.instance_name:
            return
        registry.register(
            name=self.instance_name,
            pid=os.getpid(),
            tmux_session="",
            tmux_socket="",
            toolset="",
            task="",
            conversational=True,
            session_dir="",
            role="broker",
            socket_path=self.socket_path,
            registry_dir=self.registry_dir,
        )

    def _deregister_instance(self) -> None:
        if not self.instance_name:
            return
        registry.deregister(self.instance_name, registry_dir=self.registry_dir)

    def _teardown(self) -> None:
        for conn in list(self.conns.values()):
            self._close(conn)
        if self._listener is not None:
            try:
                self.sel.unregister(self._listener)
            except (KeyError, ValueError):
                pass
            try:
                self._listener.close()
            except OSError:
                pass
        if self._wake_r is not None:
            try:
                self.sel.unregister(self._wake_r)
            except (KeyError, ValueError):
                pass
            try:
                os.close(self._wake_r)
            except OSError:
                pass
        if self._wake_w is not None:
            try:
                os.close(self._wake_w)
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self._deregister_instance()
        self.sel.close()
