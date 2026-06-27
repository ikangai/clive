"""Tests for event-driven await_ready_events path.

Exercises the ByteEvent-based completion detection that bypasses the
polling wait_for_ready path.
"""
import asyncio
import time
import pytest
from unittest.mock import MagicMock

from byte_classifier import ByteEvent
from completion import await_ready_events
from models import PaneInfo


def _fake_pane(screen_content: str):
    pane = MagicMock()
    pane.cmd.return_value.stdout = screen_content.splitlines()
    return pane


def _activity_event():
    """A neutral ByteEvent — proves the stream is alive but is neither a
    completion marker nor an intervention (so the loop keeps waiting)."""
    return ByteEvent(
        kind="color_alert", match_bytes=b"", stream_offset=0, timestamp=0.0,
    )


def _marker_event(marker_bytes: bytes):
    return ByteEvent(
        kind="cmd_end", match_bytes=b"EXIT:0 " + marker_bytes,
        stream_offset=0, timestamp=0.0,
    )


@pytest.mark.asyncio
async def test_returns_on_cmd_end_event():
    pane = _fake_pane("output\nEXIT:0 ___DONE_abc\n[AGENT_READY] $ ")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell")
    q = asyncio.Queue()
    q.put_nowait(ByteEvent(
        kind="cmd_end",
        match_bytes=b"EXIT:0 ___DONE_abc",
        stream_offset=0,
        timestamp=0.0,
    ))

    screen, method = await await_ready_events(
        info, marker="___DONE_abc", event_source=q, max_wait=2.0,
    )
    assert method == "marker"
    assert "EXIT:0" in screen


@pytest.mark.asyncio
async def test_returns_on_intervention_event():
    pane = _fake_pane("sudo -S\nPassword: ")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell")
    q = asyncio.Queue()
    q.put_nowait(ByteEvent(
        kind="password_prompt",
        match_bytes=b"Password: ",
        stream_offset=0,
        timestamp=0.0,
    ))

    screen, method = await await_ready_events(
        info, event_source=q, detect_intervention=True, max_wait=2.0,
    )
    assert method == "intervention:password_prompt"


@pytest.mark.asyncio
async def test_idle_timeout_when_no_events():
    pane = _fake_pane("still thinking...")
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        idle_timeout=0.2,
    )
    q = asyncio.Queue()
    screen, method = await await_ready_events(info, event_source=q, max_wait=0.5)
    assert method in ("idle", "max_wait")


def test_wait_for_ready_without_event_source_still_polls():
    """Sanity: existing poll path is unchanged when event_source is None."""
    from completion import wait_for_ready
    pane = MagicMock()
    pane.cmd.return_value.stdout = ["[AGENT_READY] $"]
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        idle_timeout=0.1,
    )
    screen, method = wait_for_ready(info, max_wait=1.0)
    assert method == "prompt"


@pytest.mark.asyncio
async def test_max_wait_enforced_even_when_smaller_than_idle():
    """max_wait must not overrun when smaller than idle_timeout."""
    pane = _fake_pane("")
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        idle_timeout=2.0,
    )
    q = asyncio.Queue()
    t0 = time.monotonic()
    screen, method = await await_ready_events(info, event_source=q, max_wait=0.2)
    elapsed = time.monotonic() - t0
    assert method == "max_wait"
    assert elapsed < 1.0  # proves we didn't wait the full idle_timeout


@pytest.mark.asyncio
async def test_error_keyword_event_maps_to_fatal_error():
    pane = _fake_pane("Traceback (most recent call last):")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell")
    q = asyncio.Queue()
    q.put_nowait(ByteEvent(
        kind="error_keyword",
        match_bytes=b"Traceback",
        stream_offset=0,
        timestamp=0.0,
    ))
    screen, method = await await_ready_events(
        info, event_source=q, detect_intervention=True, max_wait=1.0,
    )
    assert method == "intervention:fatal_error"


@pytest.mark.asyncio
async def test_live_stream_not_abandoned_at_soft_max_wait(monkeypatch):
    """Activity-aware max_wait: events arriving faster than the idle window
    past a small max_wait keep a slow-but-LIVE command alive. It must NOT
    be finalized 'max_wait' at the soft ceiling — here it stays alive long
    enough to detect the marker that arrives well past max_wait."""
    import completion
    monkeypatch.setattr(completion, "MAX_WAIT_HARD", 5.0, raising=False)
    pane = _fake_pane("working...\nEXIT:0 ___DONE_live")
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        idle_timeout=0.5,
    )
    q = asyncio.Queue()

    async def producer():
        for _ in range(6):  # spans past max_wait=0.15, gaps (0.05) << idle
            await asyncio.sleep(0.05)
            q.put_nowait(_activity_event())
        await asyncio.sleep(0.05)
        q.put_nowait(_marker_event(b"___DONE_live"))

    prod = asyncio.create_task(producer())
    screen, method = await await_ready_events(
        info, marker="___DONE_live", event_source=q, max_wait=0.15,
    )
    await prod
    # The marker arrived ~0.35s in, far past max_wait=0.15. Reaching it
    # proves the live stream was not abandoned at the soft ceiling.
    assert method == "marker"


@pytest.mark.asyncio
async def test_stream_going_idle_past_max_wait_returns_max_wait(monkeypatch):
    """A stream that keeps the command alive past max_wait and then stops
    is finalized 'max_wait' only after the idle window — not at the soft
    ceiling. (Buggy behavior would return ~max_wait=0.2 immediately.)"""
    import completion
    monkeypatch.setattr(completion, "MAX_WAIT_HARD", 5.0, raising=False)
    pane = _fake_pane("output")
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        idle_timeout=0.3,
    )
    q = asyncio.Queue()

    async def producer():
        for _ in range(6):  # last event ~0.30, well past max_wait=0.2
            await asyncio.sleep(0.05)
            q.put_nowait(_activity_event())
        # then stop feeding -> the stream goes idle

    prod = asyncio.create_task(producer())
    t0 = time.monotonic()
    screen, method = await await_ready_events(info, event_source=q, max_wait=0.2)
    elapsed = time.monotonic() - t0
    await prod
    assert method == "max_wait"
    # Returned only after the idle window past the last event (~0.6s), i.e.
    # well beyond the soft max_wait=0.2 ceiling.
    assert elapsed >= 0.45
    assert elapsed < 3.0


@pytest.mark.asyncio
async def test_endless_stream_capped_at_hard_backstop(monkeypatch):
    """A stream that floods events forever past max_wait is still abandoned
    'max_wait' once the hard backstop MAX_WAIT_HARD is exceeded — it is
    capped, not blocked indefinitely by a never-ending spinner."""
    import completion
    monkeypatch.setattr(completion, "MAX_WAIT_HARD", 0.6, raising=False)
    pane = _fake_pane("spinning...")
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        idle_timeout=0.4,
    )
    q = asyncio.Queue()

    async def flood():
        try:
            while True:
                await asyncio.sleep(0.02)  # gaps << idle -> never idle
                q.put_nowait(_activity_event())
        except asyncio.CancelledError:
            pass

    prod = asyncio.create_task(flood())
    t0 = time.monotonic()
    screen, method = await await_ready_events(info, event_source=q, max_wait=0.1)
    elapsed = time.monotonic() - t0
    prod.cancel()
    await prod
    assert method == "max_wait"
    # Capped at the hard backstop (~0.6), not the soft max_wait (0.1) and
    # not unbounded.
    assert elapsed >= 0.5
    assert elapsed < 2.0


def test_wait_for_ready_event_source_from_sync_context():
    """Sync-bridge path: wait_for_ready(event_source=q) with no running loop."""
    from completion import wait_for_ready
    pane = _fake_pane("EXIT:0 ___DONE_x")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell")
    q = asyncio.Queue()
    q.put_nowait(ByteEvent(
        kind="cmd_end",
        match_bytes=b"EXIT:0 ___DONE_x",
        stream_offset=0,
        timestamp=0.0,
    ))
    screen, method = wait_for_ready(
        info, marker="___DONE_x", event_source=q, max_wait=1.0,
    )
    assert method == "marker"
