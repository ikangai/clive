"""Selectors-based conversational event loop (Phase 0).

Replaces the blocking ``sys.stdin.readline()`` loop in
conversational mode with a multiplex over any number of readable
sources. Same single-line-at-a-time handler contract as before, plus
the ability to register additional fds (lobby pane reader in Phase 4,
etc.) without another refactor.

Shape:

    loop = ConvLoop()
    loop.on_line(sys.stdin, handle_task_line)   # handle_task_line(str) -> bool
    loop.run()                                   # blocks until EOF / stop / True

Design notes:

* Line framing is implemented with raw ``os.read`` + a per-source
  byte buffer, not ``file.readline()``. Python's text buffer can
  stash bytes past a line terminator such that ``select()`` reports
  nothing readable while more lines sit in the buffer — a real
  correctness issue for a select-driven loop. Raw reads on
  non-blocking fds sidestep it.

* A self-pipe lets ``stop()`` wake ``select()`` from any thread
  (signal handlers, tests, future admin commands).

* Handler exceptions are logged and swallowed so a bad task does not
  tear down the keep-alive loop — that preserves the contract of the
  pre-refactor loop, which emitted failure frames but did not exit.

See ``docs/plans/2026-04-14-clive-rooms-design.md`` §6.1.
"""
from __future__ import annotations

import logging
import os
import selectors
from typing import Callable

_log = logging.getLogger(__name__)


class _LineSource:
    """One registered line-oriented source. Owns its own byte
    buffer; yields complete lines on each readable event."""

    __slots__ = ("fd", "handler", "buf", "closed")

    def __init__(self, fd: int, handler: Callable[[str], bool]):
        self.fd = fd
        self.handler = handler
        self.buf = b""
        self.closed = False

    def drain(self) -> list[str]:
        """Read whatever bytes are available and return any complete
        lines (each including the trailing '\\n'). Sets ``closed`` on
        EOF. Returns [] if the read would have blocked — defensive
        against spurious ``select`` readiness.

        On EOF with a non-empty buffer, the trailing bytes are
        flushed as one last synthetic line (without a '\\n'). This
        matches the pre-refactor ``sys.stdin.readline()`` contract,
        which returned a partial final line when the peer closed
        mid-line. Losing it would be a silent behaviour regression.
        """
        try:
            data = os.read(self.fd, 4096)
        except (BlockingIOError, InterruptedError):
            return []
        except OSError:
            data = b""
        if not data:
            self.closed = True
            if self.buf:
                tail = self.buf.decode("utf-8", errors="replace")
                self.buf = b""
                return [tail]
            return []
        self.buf += data
        lines: list[str] = []
        while b"\n" in self.buf:
            nl = self.buf.index(b"\n")
            line = self.buf[:nl + 1].decode("utf-8", errors="replace")
            self.buf = self.buf[nl + 1:]
            lines.append(line)
        return lines


class ConvLoop:
    """Single-threaded selector loop for conversational mode.

    Public surface:

    ``on_line(stream_or_fd, handler)``
        Register a line-delimited readable. ``handler(line: str)`` is
        invoked for each complete line; if it returns ``True`` the
        loop stops. Exceptions are logged, swallowed, and the loop
        continues.

    ``run()``
        Block until ``stop()`` is called OR every registered source
        has hit EOF. Safe against spurious wake-ups.

    ``stop()``
        Thread-safe signal to end the loop at the next iteration.
    """

    def __init__(self):
        self._sel = selectors.DefaultSelector()
        self._sources: dict[int, _LineSource] = {}
        # Per-registered-fd snapshot of the original blocking flag so
        # we can restore it on teardown. Callers typically pass
        # ``sys.stdin``, and flipping it to non-blocking permanently
        # would surprise downstream code (and leak across pytest test
        # boundaries when the same interpreter is reused).
        self._original_blocking: dict[int, bool] = {}
        self._stop = False
        # Self-pipe — a write to `_wake_w` makes an in-flight
        # `select()` return so `stop()` is prompt.
        r, w = os.pipe()
        os.set_blocking(r, False)
        self._wake_r = r
        self._wake_w = w
        self._sel.register(r, selectors.EVENT_READ, data=("wake", None))

    # ─── Registration ───────────────────────────────────────────────

    def on_line(self, stream, handler: Callable[[str], bool]) -> None:
        """Register a readable source. `stream` may be a file-like
        object (sys.stdin, an `os.fdopen()` wrapper) or a raw fd.
        The handler receives each complete line as a str including
        the trailing newline."""
        fd = stream.fileno() if hasattr(stream, "fileno") else int(stream)
        # Snapshot the original blocking state before we flip it so
        # teardown can restore it — otherwise a test that registers
        # sys.stdin silently leaks non-blocking into subsequent tests.
        try:
            self._original_blocking[fd] = os.get_blocking(fd)
        except OSError:
            self._original_blocking[fd] = True
        # Non-blocking so os.read returns promptly on spurious readiness.
        os.set_blocking(fd, False)
        src = _LineSource(fd, handler)
        self._sources[fd] = src
        self._sel.register(fd, selectors.EVENT_READ, data=("line", src))

    # ─── Lifecycle ──────────────────────────────────────────────────

    def stop(self) -> None:
        """Request the loop to exit at the next iteration. Thread-safe."""
        self._stop = True
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    def run(self) -> None:
        """Run until ``stop()`` is called or all sources hit EOF."""
        try:
            while not self._stop and self._sources:
                events = self._sel.select(timeout=None)
                for key, _ in events:
                    tag, payload = key.data
                    if tag == "wake":
                        try:
                            os.read(self._wake_r, 4096)
                        except OSError:
                            pass
                        continue
                    src: _LineSource = payload
                    lines = src.drain()
                    for line in lines:
                        try:
                            if src.handler(line) is True:
                                self._stop = True
                                break
                        except Exception as e:
                            _log.warning(
                                "conv_loop: handler raised %s — continuing",
                                e, exc_info=True,
                            )
                    if src.closed:
                        self._drop(src.fd)
                    if self._stop:
                        break
        finally:
            self._teardown()

    # ─── Internals ──────────────────────────────────────────────────

    def _drop(self, fd: int) -> None:
        src = self._sources.pop(fd, None)
        if src is None:
            return
        try:
            self._sel.unregister(fd)
        except (KeyError, ValueError):
            pass
        prev = self._original_blocking.pop(fd, None)
        if prev is not None:
            try:
                os.set_blocking(fd, prev)
            except OSError:
                pass

    def _teardown(self) -> None:
        for fd in list(self._sources):
            self._drop(fd)
        if self._wake_r is not None:
            try:
                self._sel.unregister(self._wake_r)
            except (KeyError, ValueError):
                pass
            try:
                os.close(self._wake_r)
            except OSError:
                pass
            self._wake_r = None
        if self._wake_w is not None:
            try:
                os.close(self._wake_w)
            except OSError:
                pass
            self._wake_w = None
        self._sel.close()
