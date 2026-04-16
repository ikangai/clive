"""Tests for event-driven await_ready_events path.

Exercises the ByteEvent-based completion detection that bypasses the
polling wait_for_ready path.
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from byte_classifier import ByteEvent
from completion import await_ready_events
from models import PaneInfo


def _fake_pane(screen_content: str):
    pane = MagicMock()
    pane.cmd.return_value.stdout = screen_content.splitlines()
    return pane


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
