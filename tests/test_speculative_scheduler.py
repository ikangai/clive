"""Unit tests for SpeculationScheduler — LLM client and pane_loop mocked."""
import time
import pytest
from unittest.mock import MagicMock

from byte_classifier import ByteEvent
from speculative import SpeculationScheduler, SPEC_TRIGGERS, SpecCall


def _evt(kind="cmd_end"):
    return ByteEvent(kind=kind, match_bytes=b"X", stream_offset=0, timestamp=0.0)


def _ready_future(reply="answer", pt=100, ct=50):
    """Create a done-Future that returns (reply, pt, ct)."""
    f = MagicMock()
    f.done.return_value = True
    f.result.return_value = (reply, pt, ct)
    f.cancel.return_value = False
    return f


def _pending_future():
    f = MagicMock()
    f.done.return_value = False
    f.cancel.return_value = False
    return f


def _make_pane_loop(futs: list):
    """pane_loop.submit() returns successive futures from `futs`.

    Also closes any coroutine handed to submit so we don't leave orphan
    coroutines behind (production PaneLoop schedules them; MagicMock
    doesn't).
    """
    pl = MagicMock()
    it = iter(futs)

    def _submit(coro):
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass
        return next(it)

    pl.submit.side_effect = _submit
    return pl


def test_spec_triggers_cover_high_confidence_only():
    # Spec: cmd_end + intervention kinds only; not color_alert / blink
    assert "cmd_end" in SPEC_TRIGGERS
    assert "password_prompt" in SPEC_TRIGGERS
    assert "confirm_prompt" in SPEC_TRIGGERS
    assert "error_keyword" in SPEC_TRIGGERS
    assert "permission_error" in SPEC_TRIGGERS
    # Negative
    assert "color_alert" not in SPEC_TRIGGERS
    assert "blink_attr" not in SPEC_TRIGGERS


def test_fire_records_inflight_and_increments_version():
    client = MagicMock()
    pl = _make_pane_loop([_pending_future()])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    assert sched.fire(_evt(), messages_snapshot=[]) is True
    assert len(sched.in_flight) == 1
    assert sched.in_flight[0].version == 1


def test_rate_limit_rejects_rapid_second_fire():
    client = MagicMock()
    pl = _make_pane_loop([_pending_future(), _pending_future()])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    assert sched.fire(_evt(), messages_snapshot=[]) is True
    # immediately — should be dropped
    assert sched.fire(_evt(), messages_snapshot=[]) is False
    # after MIN_FIRE_INTERVAL — OK
    time.sleep(sched.MIN_FIRE_INTERVAL + 0.02)
    assert sched.fire(_evt(), messages_snapshot=[]) is True


def test_max_in_flight_cancels_oldest():
    client = MagicMock()
    f1, f2, f3 = _pending_future(), _pending_future(), _pending_future()
    pl = _make_pane_loop([f1, f2, f3])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    sched.MIN_FIRE_INTERVAL = 0.0  # disable rate limit for this test

    sched.fire(_evt(), messages_snapshot=[])
    sched.fire(_evt(), messages_snapshot=[])
    # Third fire forces oldest (v=1) to cancel
    sched.fire(_evt(), messages_snapshot=[])
    f1.cancel.assert_called_once()
    assert len(sched.in_flight) == sched.MAX_IN_FLIGHT


def test_try_consume_returns_latest_completed():
    client = MagicMock()
    fut = _ready_future(reply="hello", pt=10, ct=5)
    pl = _make_pane_loop([fut])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    sched.fire(_evt(), messages_snapshot=[])
    reply, pt, ct = sched.try_consume(current_messages=[])
    assert reply == "hello"
    assert pt == 10
    assert ct == 5
    assert sched.accepted_version == 1


def test_try_consume_skips_pending():
    client = MagicMock()
    fut = _pending_future()
    pl = _make_pane_loop([fut])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    sched.fire(_evt(), messages_snapshot=[])
    reply, pt, ct = sched.try_consume(current_messages=[])
    assert reply is None


def test_try_consume_rejects_snapshot_mismatch():
    """If the messages prefix diverged between fire and consume, reject."""
    client = MagicMock()
    fut = _ready_future(reply="stale", pt=100, ct=50)
    pl = _make_pane_loop([fut])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    old_msgs = [{"role": "user", "content": "old"}]
    sched.fire(_evt(), messages_snapshot=old_msgs)
    # Caller's messages no longer match the snapshot
    new_msgs = [{"role": "user", "content": "new"}]
    reply, pt, ct = sched.try_consume(current_messages=new_msgs)
    assert reply is None


def test_try_consume_rejects_below_accepted_version():
    """A result whose version <= accepted_version is dropped."""
    client = MagicMock()
    fut1 = _ready_future(reply="first")
    fut2 = _ready_future(reply="second")
    pl = _make_pane_loop([fut1, fut2])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    sched.MIN_FIRE_INTERVAL = 0.0

    sched.fire(_evt(), messages_snapshot=[])
    sched.fire(_evt(), messages_snapshot=[])

    # Consume accepts v=2 (the latest)
    reply, pt, ct = sched.try_consume(current_messages=[])
    assert reply == "second"
    assert sched.accepted_version == 2

    # Second consume finds nothing to accept
    reply2, _, _ = sched.try_consume(current_messages=[])
    assert reply2 is None


def test_accept_cancels_older_calls():
    """When a call is accepted, older in-flight calls are cancelled."""
    client = MagicMock()
    older = _pending_future()       # older, still running
    newer = _ready_future(reply="newer")  # newer, done
    pl = _make_pane_loop([older, newer])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    sched.MIN_FIRE_INTERVAL = 0.0

    sched.fire(_evt(), messages_snapshot=[])  # v=1 (older, pending)
    sched.fire(_evt(), messages_snapshot=[])  # v=2 (newer, ready)

    sched.try_consume(current_messages=[])
    older.cancel.assert_called_once()


def test_metrics_snapshot_tracks_fires_accepts_and_cancellations():
    client = MagicMock()
    fut = _ready_future(reply="ok")
    pl = _make_pane_loop([fut])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    sched.MIN_FIRE_INTERVAL = 0.0  # disable rate limit for deterministic counting

    sched.fire(_evt(), messages_snapshot=[])
    sched.try_consume(current_messages=[])

    m = sched.snapshot_metrics()
    assert m["fires_total"] == 1
    assert m["accepts_total"] == 1
    assert m["discards_snapshot_mismatch"] == 0
    assert m["cancellations_total"] == 0


def test_metrics_snapshot_tracks_rate_limit_and_mismatch():
    client = MagicMock()
    fut_stale = _ready_future(reply="stale")
    pl = _make_pane_loop([fut_stale, _pending_future()])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    # Keep rate limit in force: second fire immediately after first

    sched.fire(_evt(), messages_snapshot=[{"role": "user", "content": "snap"}])
    sched.fire(_evt(), messages_snapshot=[])  # rate-limited
    sched.try_consume(current_messages=[{"role": "user", "content": "different"}])  # mismatch

    m = sched.snapshot_metrics()
    assert m["fires_total"] == 1
    assert m["fires_rate_limited"] == 1
    assert m["discards_snapshot_mismatch"] == 1


def test_circuit_breaker_disables_after_threshold():
    client = MagicMock()
    pl = _make_pane_loop([_pending_future() for _ in range(20)])
    sched = SpeculationScheduler(client, model="test", pane_loop=pl)
    sched.MIN_FIRE_INTERVAL = 0.0

    # Fabricate BREAKER_THRESHOLD + 1 cancellations in quick succession
    for _ in range(sched.BREAKER_THRESHOLD + 1):
        sched._record_cancel()

    # Next fire should be dropped (breaker tripped)
    fired = sched.fire(_evt(), messages_snapshot=[])
    assert fired is False
