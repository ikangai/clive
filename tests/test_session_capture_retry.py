"""Tests for bounded transient-retry on tmux capture-pane reads in session.py.

These exercise the module-level ``_pane_cmd_with_retry`` helper and verify
that ``capture_pane`` / ``check_health`` route their ``capture-pane`` reads
through it. No real tmux is used — panes are tiny fakes whose ``.cmd`` we
script to raise transient errors a set number of times.
"""
import pytest

import session
from session import _pane_cmd_with_retry, capture_pane, check_health


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


# --- _pane_cmd_with_retry helper ------------------------------------------

def test_retry_succeeds_after_transient_failures():
    """Two OSError hiccups then success: result returned, cmd called 3 times."""
    pane = _FlakyPane(fail_times=2, result=_Result(["hello"]))

    result = _pane_cmd_with_retry(
        pane, "capture-pane", "-p", sleep_fn=_no_sleep
    )

    assert result.stdout == ["hello"]
    assert len(pane.calls) == 3
    assert pane.calls[0] == ("capture-pane", "-p")


def test_retry_reraises_after_attempts_exhausted():
    """A pane that always raises propagates the error after `attempts` tries."""
    pane = _FlakyPane(fail_times=99, result=None)

    with pytest.raises(OSError):
        _pane_cmd_with_retry(
            pane, "capture-pane", "-p", attempts=3, sleep_fn=_no_sleep
        )

    assert len(pane.calls) == 3


def test_retry_backoff_delays_are_exponential():
    """sleep_fn is called between attempts with exponential base_delay * 2**i."""
    pane = _FlakyPane(fail_times=2, result=_Result(["ok"]))
    delays = []

    _pane_cmd_with_retry(
        pane, "capture-pane", attempts=3, base_delay=0.1, sleep_fn=delays.append
    )

    # Two failures => two sleeps, no sleep after the final success.
    assert delays == [0.1, 0.2]


def test_retry_does_not_swallow_unexpected_errors():
    """Non-transient errors (e.g. ValueError) propagate immediately, no retry."""
    pane = _FlakyPane(fail_times=99, result=None, exc=ValueError)

    with pytest.raises(ValueError):
        _pane_cmd_with_retry(pane, "capture-pane", sleep_fn=_no_sleep)

    assert len(pane.calls) == 1


# --- capture_pane / check_health routing through the helper ----------------

class _FakePaneInfo:
    def __init__(self, pane, app_type="shell", description="d"):
        self.pane = pane
        self.app_type = app_type
        self.description = description


def _patch_default_sleep(monkeypatch):
    """Make the helper's bound ``sleep_fn`` default a no-op (instant tests).

    ``sleep_fn=time.sleep`` is captured as a keyword default at definition
    time, so patching ``session.time.sleep`` would not intercept it — the
    default lives in ``__kwdefaults__``. capture_pane/check_health call the
    helper without passing sleep_fn, so this is how they're sped up.
    """
    monkeypatch.setitem(
        session._pane_cmd_with_retry.__kwdefaults__, "sleep_fn", _no_sleep
    )


def test_capture_pane_survives_transient_error(monkeypatch):
    """capture_pane retries past a transient hiccup and returns screen text."""
    _patch_default_sleep(monkeypatch)
    pane = _FlakyPane(fail_times=1, result=_Result(["line one", "line two"]))
    info = _FakePaneInfo(pane)

    out = capture_pane(info)

    assert out == "line one\nline two"
    assert len(pane.calls) == 2


def test_check_health_survives_transient_error(monkeypatch):
    """check_health retries the capture-pane read and reports readiness."""
    _patch_default_sleep(monkeypatch)
    pane = _FlakyPane(fail_times=1, result=_Result(["foo", "[AGENT_READY]", "bar"]))
    info = _FakePaneInfo(pane)

    status = check_health({"shell": info})

    assert status["shell"]["status"] == "ready"
    assert len(pane.calls) == 2
