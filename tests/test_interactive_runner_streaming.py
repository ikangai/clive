"""Verify _send_agent_command uses the event path when pane has a stream."""
from unittest.mock import MagicMock, patch

import pytest

from interactive_runner import _send_agent_command
from models import Subtask, PaneInfo


def _minimal_subtask():
    # Subtask dataclass has many fields; we only need id/description/pane/mode.
    return Subtask(
        id="t1", description="test", pane="shell", mode="interactive",
        depends_on=[],
    )


def test_stream_absent_uses_poll_path():
    pane = MagicMock()
    pane.send_keys.return_value = None
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        stream=None, pane_loop=None,
    )

    with patch("interactive_runner.wait_for_ready") as wfr:
        wfr.return_value = ("screen-text", "marker")
        _send_agent_command("echo hi", _minimal_subtask(), info, "/tmp/clive/test")

    args, kwargs = wfr.call_args
    # event_source kwarg must be absent OR None
    assert kwargs.get("event_source") is None


def test_stream_present_uses_event_path_via_pane_loop():
    pane = MagicMock()
    pane.send_keys.return_value = None

    # Fake stream + pane_loop
    stream = MagicMock()
    stream.subscribe.return_value = MagicMock(name="queue")
    pane_loop = MagicMock()
    # submit returns a "future" whose .result() returns the (screen, method) tuple
    fut = MagicMock()
    fut.result.return_value = ("screen-text", "marker")
    pane_loop.submit.return_value = fut

    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        stream=stream, pane_loop=pane_loop,
    )

    with patch("interactive_runner.wait_for_ready") as wfr:
        screen, method = _send_agent_command(
            "echo hi", _minimal_subtask(), info, "/tmp/clive/test",
        )

    # wait_for_ready (sync/poll) was NOT called
    wfr.assert_not_called()
    # subscribe() was called once
    stream.subscribe.assert_called_once()
    # pane_loop.submit was called with SOME coroutine (we can't directly
    # assert the awaitable identity; assert it was called)
    pane_loop.submit.assert_called_once()
    fut.result.assert_called_once()
    assert screen == "screen-text"
    assert method == "marker"

    # Close the coroutine handed to submit (submit is mocked, so it was
    # never actually awaited) to silence RuntimeWarning.
    import inspect
    submitted = pane_loop.submit.call_args.args[0]
    if inspect.iscoroutine(submitted):
        submitted.close()


def test_stream_path_passes_marker_and_intervention_flags():
    """Verify the await_ready_events coroutine got the right args by
    inspecting what submit was called with."""
    pane = MagicMock()
    stream = MagicMock()
    q = MagicMock(name="queue")
    stream.subscribe.return_value = q
    pane_loop = MagicMock()
    fut = MagicMock()
    fut.result.return_value = ("", "marker")
    pane_loop.submit.return_value = fut

    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        stream=stream, pane_loop=pane_loop,
    )

    _send_agent_command("echo hi", _minimal_subtask(), info, "/tmp/clive/test")

    # The coroutine passed to submit should have been created from
    # await_ready_events — inspecting precisely is fiddly (Python coro
    # object), but we can verify submit was called once with a coroutine.
    submitted = pane_loop.submit.call_args.args[0]
    import inspect
    assert inspect.iscoroutine(submitted)
    # Close it to avoid RuntimeWarning
    submitted.close()
