"""Tests that the two hot capture-pane reads in observation/completion.py
route through session._pane_cmd_with_retry.

session._pane_cmd_with_retry exists so a single libtmux/subprocess glitch on
a capture-pane read doesn't propagate uncaught and abort the whole subtask.
Before this change it wrapped only capture_pane() / check_health(); the two
highest-frequency captures — the _wait_polling poll read (many times per
command) and await_ready_events' final screen capture (once per command in
the default streaming path) — were unprotected. These tests script tiny fake
panes whose ``.cmd`` raises a transient error before succeeding and assert
both sites survive it. No real tmux is used.
"""
import asyncio

import pytest

import session
from completion import await_ready_events, wait_for_ready
from models import PaneInfo


class _Result:
    """Stand-in for a libtmux cmd result: only ``.stdout`` is read."""

    def __init__(self, lines):
        self.stdout = lines


class _FlakyPane:
    """Fake pane: ``.cmd`` raises ``exc`` ``fail_times`` times, then returns ``result``."""

    def __init__(self, fail_times, result, exc=OSError):
        self.fail_times = fail_times
        self.result = result
        self.exc = exc
        self.calls = []

    def cmd(self, *args):
        self.calls.append(args)
        if len(self.calls) <= self.fail_times:
            raise self.exc("transient tmux hiccup")
        return self.result


def _no_sleep(_delay):
    """sleep_fn that records nothing and returns instantly."""


def _patch_default_sleep(monkeypatch):
    """Make the retry helper's bound ``sleep_fn`` default a no-op.

    ``sleep_fn=time.sleep`` is captured as a keyword default at definition
    time, so it lives in ``__kwdefaults__``. completion.py calls the helper
    without passing sleep_fn, so this is how the retries run instantly.
    """
    monkeypatch.setitem(
        session._pane_cmd_with_retry.__kwdefaults__, "sleep_fn", _no_sleep
    )


# --- poll path: _wait_polling --------------------------------------------

def test_poll_capture_survives_transient_error(monkeypatch):
    """The poll read retries past a transient hiccup, then detects the prompt."""
    _patch_default_sleep(monkeypatch)
    pane = _FlakyPane(
        fail_times=1, result=_Result(["output", "[AGENT_READY] $"])
    )
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        idle_timeout=0.1,
    )

    screen, method = wait_for_ready(info, max_wait=1.0)

    assert method == "prompt"
    assert "[AGENT_READY] $" in screen
    # One failure + one success on the single poll iteration that detects ready.
    assert len(pane.calls) == 2
    assert pane.calls[0] == ("capture-pane", "-p", "-J")


# --- streaming path: await_ready_events final capture --------------------

@pytest.mark.asyncio
async def test_event_final_capture_survives_transient_error(monkeypatch):
    """await_ready_events' final screen capture retries past a transient hiccup."""
    _patch_default_sleep(monkeypatch)
    pane = _FlakyPane(fail_times=1, result=_Result(["final", "screen"]))
    info = PaneInfo(
        pane=pane, app_type="shell", description="", name="shell",
        idle_timeout=0.05,
    )
    q = asyncio.Queue()  # empty -> loop goes idle, then takes the final capture

    screen, method = await await_ready_events(info, event_source=q, max_wait=0.2)

    assert method in ("idle", "max_wait")
    assert screen == "final\nscreen"
    # The only pane.cmd calls are the final capture: one failure + one success.
    assert len(pane.calls) == 2
    assert pane.calls[0] == ("capture-pane", "-p", "-J")
