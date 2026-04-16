"""Tests for PaneLoop — per-pane asyncio loop on a background thread."""
import asyncio
import time

import pytest
from pane_loop import PaneLoop


def test_submit_and_get_result():
    loop = PaneLoop.start()
    try:
        async def work():
            await asyncio.sleep(0.01)
            return 42
        fut = loop.submit(work())
        assert fut.result(timeout=1.0) == 42
    finally:
        loop.stop()


def test_loop_stops_cleanly():
    loop = PaneLoop.start()
    loop.stop()
    assert not loop.thread.is_alive()


def test_submit_after_stop_raises():
    loop = PaneLoop.start()
    loop.stop()
    coro = asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError):
            loop.submit(coro)
    finally:
        coro.close()


def test_multiple_concurrent_submits():
    """Verify the loop serves multiple coroutines concurrently."""
    loop = PaneLoop.start()
    try:
        async def work(n):
            await asyncio.sleep(0.05)
            return n * 2
        futs = [loop.submit(work(i)) for i in range(5)]
        results = sorted(f.result(timeout=2.0) for f in futs)
        assert results == [0, 2, 4, 6, 8]
    finally:
        loop.stop()


def test_stop_is_idempotent():
    """Calling stop() twice should not raise."""
    loop = PaneLoop.start()
    loop.stop()
    loop.stop()  # should be safe


def test_submit_propagates_exception():
    """An exception inside the submitted coroutine re-raises on .result()."""
    loop = PaneLoop.start()
    try:
        async def boom():
            raise ValueError("nope")
        fut = loop.submit(boom())
        with pytest.raises(ValueError, match="nope"):
            fut.result(timeout=1.0)
    finally:
        loop.stop()


def test_stop_cancels_running_task():
    """stop() must cancel an in-flight coroutine (via the _run() finally
    block's gather-with-cancellation), not block for the coroutine's natural
    duration."""
    loop = PaneLoop.start()
    async def long():
        await asyncio.sleep(10)
    loop.submit(long())
    time.sleep(0.05)  # let it start awaiting
    t0 = time.monotonic()
    loop.stop()
    assert time.monotonic() - t0 < 1.0, "stop() should not wait for sleep(10)"
    assert not loop.thread.is_alive()
