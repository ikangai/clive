"""Tests for PaneLoop — per-pane asyncio loop on a background thread."""
import asyncio
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
    with pytest.raises(RuntimeError):
        loop.submit(asyncio.sleep(0))


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
