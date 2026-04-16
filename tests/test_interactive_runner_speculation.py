"""Tests for speculation integration in interactive_runner.

The runner's turn loop must prefer scheduler.try_consume() over
chat_stream() when an accepted speculative result is available.
"""
from unittest.mock import MagicMock, patch
import pytest

from interactive_runner import run_subtask_interactive
from models import Subtask, PaneInfo, SubtaskStatus


def _minimal_subtask():
    return Subtask(
        id="t1", description="test subtask", pane="shell",
        mode="interactive", depends_on=[], max_turns=3,
    )


def _pane_info_no_stream():
    pane = MagicMock()
    pane.send_keys.return_value = None
    pane.cmd.return_value.stdout = ["[AGENT_READY] $ "]
    return PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        stream=None, pane_loop=None,
    )


def _pane_info_with_stream():
    pane = MagicMock()
    pane.send_keys.return_value = None
    pane.cmd.return_value.stdout = ["[AGENT_READY] $ "]

    # Stream whose subscribe() returns a mock queue
    stream = MagicMock()
    stream.subscribe.return_value = MagicMock()

    # Pane loop whose submit() returns a Future
    pane_loop = MagicMock()
    watch_future = MagicMock()
    watch_future.done.return_value = False
    pane_loop.submit.return_value = watch_future

    return PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        stream=stream, pane_loop=pane_loop,
    )


def test_no_stream_path_does_not_construct_scheduler():
    """When the pane has no stream, the runner must not reference
    SpeculationScheduler at all (Phase 1 behavior unchanged)."""
    info = _pane_info_no_stream()
    subtask = _minimal_subtask()

    with patch("interactive_runner.SpeculationScheduler") as SchedCls, \
         patch("interactive_runner.chat_stream") as cs, \
         patch("interactive_runner.extract_command") as ec, \
         patch("interactive_runner.extract_done", return_value="done-summary"):
        cs.return_value = ("DONE: ok", 0, 0)
        ec.return_value = None
        run_subtask_interactive(subtask, info, "dep ctx", session_dir="/tmp/x")

    SchedCls.assert_not_called()


def test_stream_path_constructs_scheduler_and_spawns_watch():
    """With a stream + pane_loop, the runner constructs a scheduler and
    spawns _spec_watch on the loop."""
    info = _pane_info_with_stream()
    subtask = _minimal_subtask()

    with patch("interactive_runner.SpeculationScheduler") as SchedCls, \
         patch("interactive_runner.chat_stream") as cs, \
         patch("interactive_runner.extract_command") as ec, \
         patch("interactive_runner.extract_done", return_value="done-summary"):
        sched_inst = MagicMock()
        sched_inst.try_consume.return_value = (None, 0, 0)
        SchedCls.return_value = sched_inst
        cs.return_value = ("DONE: ok", 10, 5)
        ec.return_value = None

        run_subtask_interactive(subtask, info, "dep ctx", session_dir="/tmp/x")

    SchedCls.assert_called_once()
    # pane_loop.submit was invoked at least once (the _spec_watch task)
    info.pane_loop.submit.assert_called()
    # try_consume was consulted on each turn
    assert sched_inst.try_consume.call_count >= 1

    # Close any coroutines handed to submit to silence RuntimeWarning.
    import inspect
    for call in info.pane_loop.submit.call_args_list:
        arg = call.args[0] if call.args else None
        if inspect.iscoroutine(arg):
            arg.close()


def test_accepted_spec_result_short_circuits_chat_stream():
    """When try_consume returns a non-None reply, chat_stream is NOT called."""
    info = _pane_info_with_stream()
    subtask = _minimal_subtask()

    with patch("interactive_runner.SpeculationScheduler") as SchedCls, \
         patch("interactive_runner.chat_stream") as cs, \
         patch("interactive_runner.extract_command", return_value=None), \
         patch("interactive_runner.extract_done", return_value="spec-done"):
        sched_inst = MagicMock()
        # First call: speculative result ready
        sched_inst.try_consume.return_value = ("DONE: spec-done", 99, 33)
        SchedCls.return_value = sched_inst

        run_subtask_interactive(subtask, info, "dep ctx", session_dir="/tmp/x")

    cs.assert_not_called()

    # Close any coroutines handed to submit to silence RuntimeWarning.
    import inspect
    for call in info.pane_loop.submit.call_args_list:
        arg = call.args[0] if call.args else None
        if inspect.iscoroutine(arg):
            arg.close()


def test_watch_future_cancelled_on_exit():
    """Runner cancels the _spec_watch future on return."""
    info = _pane_info_with_stream()
    subtask = _minimal_subtask()

    with patch("interactive_runner.SpeculationScheduler") as SchedCls, \
         patch("interactive_runner.chat_stream", return_value=("DONE: ok", 0, 0)), \
         patch("interactive_runner.extract_command", return_value=None), \
         patch("interactive_runner.extract_done", return_value="done"):
        sched_inst = MagicMock()
        sched_inst.try_consume.return_value = (None, 0, 0)
        SchedCls.return_value = sched_inst

        run_subtask_interactive(subtask, info, "dep ctx", session_dir="/tmp/x")

    watch_fut = info.pane_loop.submit.return_value
    watch_fut.cancel.assert_called()

    # Close any coroutines handed to submit to silence RuntimeWarning.
    import inspect
    for call in info.pane_loop.submit.call_args_list:
        arg = call.args[0] if call.args else None
        if inspect.iscoroutine(arg):
            arg.close()
