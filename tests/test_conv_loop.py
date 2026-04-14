"""Tests for ``session/conv_loop.py`` — the selectors-based
conversational-mode event loop introduced in Phase 0.

Phase 0 is a pure refactor: the loop must still process task lines
from stdin one at a time, break on EOF, and break on the sentinel
words (exit/quit//stop). The new contract on top is that multiple
readable streams may be registered, and the loop multiplexes between
them — preparing for Phase 4 to register the lobby pane reader.

See docs/plans/2026-04-14-clive-rooms-design.md §6.1.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from conv_loop import ConvLoop


def _pipe_pair():
    r, w = os.pipe()
    return os.fdopen(r, "r", buffering=1), os.fdopen(w, "w", buffering=1)


# ─── Single-source behaviour ────────────────────────────────────────────────


def test_processes_lines_in_order():
    r, w = _pipe_pair()
    seen: list[str] = []

    def handle(line: str) -> bool:
        seen.append(line.strip())
        return False

    loop = ConvLoop()
    loop.on_line(r, handle)

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    w.write("first\nsecond\nthird\n")
    w.flush()
    w.close()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert seen == ["first", "second", "third"]


def test_eof_closes_source_and_exits_when_last():
    r, w = _pipe_pair()
    calls = []

    def handle(line: str) -> bool:
        calls.append(line)
        return False

    loop = ConvLoop()
    loop.on_line(r, handle)

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    w.close()                     # immediate EOF
    t.join(timeout=2.0)
    assert not t.is_alive(), "loop did not exit on EOF of only source"
    assert calls == []


def test_handler_returning_true_breaks_loop():
    r, w = _pipe_pair()
    seen = []

    def handle(line: str) -> bool:
        seen.append(line.strip())
        return line.strip() == "stop"

    loop = ConvLoop()
    loop.on_line(r, handle)

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    w.write("one\n")
    w.flush()
    w.write("stop\n")
    w.flush()
    # These must not be processed because handler returned True on "stop".
    w.write("never\n")
    w.flush()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert seen == ["one", "stop"]
    w.close()


def test_handler_exception_does_not_kill_loop():
    """Per the existing conv-mode contract, a failed task run emits
    failure frames but does NOT terminate the keep-alive loop. The
    refactored loop must preserve this — handler exceptions are
    logged/swallowed, not propagated."""
    r, w = _pipe_pair()
    seen = []

    def handle(line: str) -> bool:
        seen.append(line.strip())
        if line.strip() == "boom":
            raise RuntimeError("synthetic")
        return False

    loop = ConvLoop()
    loop.on_line(r, handle)

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    w.write("ok1\nboom\nok2\n")
    w.flush()
    w.close()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert seen == ["ok1", "boom", "ok2"]


# ─── Multi-source multiplex (prepares for phase 4) ──────────────────────────


def test_two_sources_multiplexed():
    """Registering two line sources must dispatch lines from both to
    their respective handlers. This is the property phase 4 will
    depend on when it registers the lobby pane reader alongside
    stdin."""
    r1, w1 = _pipe_pair()
    r2, w2 = _pipe_pair()
    seen_1: list[str] = []
    seen_2: list[str] = []

    def h1(line: str) -> bool:
        seen_1.append(line.strip())
        return False

    def h2(line: str) -> bool:
        seen_2.append(line.strip())
        return False

    loop = ConvLoop()
    loop.on_line(r1, h1)
    loop.on_line(r2, h2)

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()

    w1.write("a1\n"); w1.flush()
    w2.write("b1\n"); w2.flush()
    w1.write("a2\n"); w1.flush()
    w2.write("b2\n"); w2.flush()
    # Close BOTH so the loop exits when no sources remain.
    w1.close()
    w2.close()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert seen_1 == ["a1", "a2"]
    assert seen_2 == ["b1", "b2"]


def test_one_source_eof_leaves_other_active():
    """Closing only one source must not bring the loop down — the
    other remains serviceable."""
    r1, w1 = _pipe_pair()
    r2, w2 = _pipe_pair()
    seen_2: list[str] = []

    loop = ConvLoop()
    loop.on_line(r1, lambda line: False)

    def h2(line: str) -> bool:
        seen_2.append(line.strip())
        return line.strip() == "done"

    loop.on_line(r2, h2)

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    w1.close()                     # first source closes immediately
    time.sleep(0.05)
    assert t.is_alive(), "loop exited despite remaining active source"
    w2.write("still-here\n"); w2.flush()
    w2.write("done\n"); w2.flush()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert seen_2 == ["still-here", "done"]
    w2.close()


def test_partial_final_line_on_eof_is_delivered():
    """When the peer writes bytes without a trailing newline and then
    closes, the refactored loop must still deliver those bytes as a
    final line — matching the behaviour of the pre-refactor
    ``sys.stdin.readline()``, which returned the partial line and then
    EOF on the next call. Dropping it silently would be a behaviour
    regression and the kind of bug that only bites under
    failure-mode traffic (a peer crashing mid-write)."""
    r, w = _pipe_pair()
    seen: list[str] = []

    def handle(line: str) -> bool:
        seen.append(line)   # do NOT strip — test raw line semantics
        return False

    loop = ConvLoop()
    loop.on_line(r, handle)

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    w.write("complete\n")   # normal line
    w.flush()
    w.write("partial")      # no trailing \n
    w.flush()
    w.close()               # EOF mid-line
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert seen == ["complete\n", "partial"]


def test_original_blocking_flag_is_restored(tmp_path):
    """ConvLoop puts registered fds into non-blocking mode. After
    teardown it MUST put them back — otherwise a test registering
    sys.stdin leaks non-blocking into the next pytest test, and in
    production any post-loop code that reads the fd behaves
    unexpectedly. This test uses a pipe so we can probe the flag
    directly without touching sys.stdin."""
    r, w = os.pipe()
    try:
        assert os.get_blocking(r) is True
        loop = ConvLoop()
        loop.on_line(r, lambda line: False)
        # Simulate a source-exhausted run.
        os.close(w)
        loop.run()
        assert os.get_blocking(r) is True, \
            "ConvLoop left fd in non-blocking state after teardown"
    finally:
        try:
            os.close(r)
        except OSError:
            pass


# ─── stop() from another thread ─────────────────────────────────────────────


def test_stop_from_outside_wakes_loop():
    """`stop()` must cause `run()` to return even if no source is
    currently readable. Useful for signal handlers."""
    r, w = _pipe_pair()

    loop = ConvLoop()
    loop.on_line(r, lambda line: False)

    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    time.sleep(0.05)
    assert t.is_alive()
    loop.stop()
    t.join(timeout=2.0)
    assert not t.is_alive(), "stop() did not wake the loop"
    w.close()
