"""Response isolation layer for concurrent pane access (gh#14).

Per-pane threading locks serialize whole subtasks: the lock covers send +
execute + wait, so independent subtasks queued on the same pane wait for
each other's full execution. This module isolates each request's *output*
instead, so only the ~1ms ``send_keys`` needs serialization and the
waiting happens concurrently.

Two pieces:

- :func:`wrap_isolated` — bookends a command with unique tags and runs it
  in a subshell ``( ... )`` so env vars, cwd changes, and aliases cannot
  leak between subtasks sharing a pane (the "context contamination"
  problem).
- :class:`PaneIsolation` — submit() returns a Future; feed() consumes the
  pane's line stream (pipe-pane / FIFO reader / gh#12 control-mode
  sidecar) and routes each tagged block to its Future. The shell executes
  sequentially, so blocks never interleave; tags make the demux exact.

Marker matching is full-line anchored: the echoed command line contains
the literal markers inside ``echo "..."`` quoting and therefore never
matches — the same trick completion.py plays with its ``EXIT:$`` guard.

Caveats (issue #14):
- Type-ahead queuing relies on commands not reading stdin; a queued
  command's bytes would be consumed by a stdin-reading predecessor.
- TUI panes can't be tagged — callers must keep whole-pane locking for
  non-shell-like ``app_type``s.

The layer is opt-in via ``CLIVE_PANE_ISOLATION=1``. Default behavior
(whole-subtask locks) is untouched.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import threading
import uuid
from concurrent.futures import Future

log = logging.getLogger(__name__)

ISOLATION_ENV = "CLIVE_PANE_ISOLATION"

_TAG_SAFE = re.compile(r"[^A-Za-z0-9_]")
_BEGIN_RE = re.compile(r"^===BEGIN_([A-Za-z0-9_]+)===\s*$")
_END_RE = re.compile(r"^===END_([A-Za-z0-9_]+)=== EXIT:(\d+)\s*$")


def isolation_enabled() -> bool:
    """True when the opt-in flag is set (gh#14 high-throughput mode)."""
    return os.environ.get(ISOLATION_ENV) == "1"


def make_tag(subtask_id: str) -> str:
    """A unique, shell-safe tag for one command submission."""
    safe = _TAG_SAFE.sub("_", subtask_id)
    return f"{safe}_{uuid.uuid4().hex[:8]}"


def wrap_isolated(cmd: str, tag: str, cwd: str | None = None) -> str:
    """Bookend ``cmd`` with tagged markers, isolated in a subshell.

    The subshell prevents env/cwd/alias leakage between subtasks sharing
    a pane; ``EXIT:$?`` right after the subshell close captures the
    command's exit code.

    Heredoc-bearing commands get newline-joined bookends: appending
    ``); echo ...`` to a heredoc terminator line corrupts it and wedges
    the pane in ``heredoc>`` mode (same bug shape gh#40 found and fixed
    in completion.py's wrap_command). Newline separation has identical
    ``$?`` semantics to ``;``.
    """
    inner = f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd
    if "<<" in inner:
        return (
            f'echo "===BEGIN_{tag}==="\n'
            f"(\n{inner}\n)\n"
            f'echo "===END_{tag}=== EXIT:$?"'
        )
    return (
        f'echo "===BEGIN_{tag}==="; '
        f"( {inner} ); "
        f'echo "===END_{tag}=== EXIT:$?"'
    )


class PaneIsolation:
    """Demultiplexes tagged command output from one pane's line stream.

    ``send_fn`` performs the actual keystroke delivery (e.g.
    ``lambda c: pane.send_keys(c, enter=True)``); it runs under a lock so
    concurrent submitters can't interleave keystrokes. Waiting on the
    returned Future is lock-free and concurrent.

    feed() is single-consumer: exactly one reader thread/coroutine per
    pane should drive it (the pipe-pane reader). The shell executes
    commands sequentially, so at most one block is open at a time.
    """

    def __init__(self, send_fn):
        self._send_fn = send_fn
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[str, Future] = {}
        self._open_tag: str | None = None
        self._buffer: list[str] = []

    def submit(self, cmd: str, tag: str, cwd: str | None = None) -> Future:
        """Send ``cmd`` wrapped with ``tag``; resolve later via feed().

        The Future resolves to ``(exit_code, output)``.
        """
        fut: Future = Future()
        with self._state_lock:
            self._pending[tag] = fut
        wrapped = wrap_isolated(cmd, tag, cwd=cwd)
        try:
            with self._send_lock:  # serialize keystrokes only, not waiting
                self._send_fn(wrapped)
        except Exception:
            # Don't leak the pending future when delivery fails.
            with self._state_lock:
                self._pending.pop(tag, None)
            raise
        return fut

    def feed(self, line: str) -> None:
        """Route one pane-output line. Lines outside tagged blocks and
        echoed command lines (which never match the full-line anchors)
        are ignored."""
        m = _BEGIN_RE.match(line)
        if m:
            with self._state_lock:
                if self._open_tag is not None:
                    log.warning(
                        "BEGIN %s while %s still open; abandoning previous block",
                        m.group(1), self._open_tag,
                    )
                self._open_tag = m.group(1)
                self._buffer = []
            return

        m = _END_RE.match(line)
        if m:
            tag, exit_code = m.group(1), int(m.group(2))
            with self._state_lock:
                fut = self._pending.pop(tag, None)
                output = "\n".join(self._buffer) if self._open_tag == tag else ""
                if self._open_tag == tag:
                    self._open_tag = None
                    self._buffer = []
            if fut is None:
                log.warning("END for unknown tag %s ignored", tag)
                return
            if not fut.done():
                fut.set_result((exit_code, output))
            return

        with self._state_lock:
            if self._open_tag is not None:
                self._buffer.append(line)

    def cancel_all(self, reason: str = "cancelled") -> None:
        """Fail every pending Future — e.g. on pane teardown."""
        with self._state_lock:
            pending, self._pending = self._pending, {}
            self._open_tag = None
            self._buffer = []
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
