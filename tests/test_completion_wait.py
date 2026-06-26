"""Tests for activity-aware max_wait in _wait_polling.

A slow-but-live command (make / npm install / large download) repaints
the pane every few seconds but may not emit the completion marker before
the soft `max_wait` ceiling. The old loop abandoned it unconditionally at
`max_wait` and returned "max_wait"; interactive_runner then typed the next
command into a still-running pane (corruption, Terminal-Bench arxiv
2601.11868).

The fix makes the absolute ceiling activity-aware: once elapsed exceeds the
soft `max_wait`, the loop only returns "max_wait" if the screen has gone
idle OR a hard backstop (MAX_WAIT_HARD) is exceeded. While the capture keeps
changing and we are under the hard cap, the command is treated as live.

These tests drive `_wait_polling` directly with a fake pane and a
monkeypatched, deterministic clock (no real sleeping).
"""
import completion
from completion import _wait_polling
from models import PaneInfo


class _Result:
    """Minimal stand-in for libtmux's cmd() result (only .stdout used)."""
    __slots__ = ("stdout",)

    def __init__(self, stdout):
        self.stdout = stdout


class FakeClock:
    """Deterministic clock. time() reads `now`; sleep() advances it.

    The poll loop's own `time.sleep(poll_interval)` drives elapsed time, so
    the test runs at full speed with no wall-clock waiting.
    """

    def __init__(self, start=1000.0):
        self.now = start
        self.start = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        # Runaway guard: a buggy fix that never returns would otherwise hang
        # the test forever. Bound the simulated clock well above any cap.
        if self.now - self.start > 10_000:
            raise RuntimeError("clock runaway: _wait_polling did not return")

    @property
    def elapsed(self):
        return self.now - self.start


class FakePane:
    """Fake tmux pane whose capture-pane stdout is produced by `screen_fn`.

    `screen_fn(elapsed, call_count)` returns the list of screen lines for
    that poll, letting a test make the screen change, go idle, or emit a
    marker as a function of simulated elapsed time.
    """

    def __init__(self, clock, screen_fn):
        self.clock = clock
        self.screen_fn = screen_fn
        self.calls = 0

    def cmd(self, *args, **kwargs):
        self.calls += 1
        return _Result(self.screen_fn(self.clock.elapsed, self.calls))


def _run(monkeypatch, screen_fn, *, marker=None, max_wait=1.0,
         hard=5.0, idle_timeout=0.3):
    clock = FakeClock()
    monkeypatch.setattr(completion.time, "time", clock.time)
    monkeypatch.setattr(completion.time, "sleep", clock.sleep)
    monkeypatch.setattr(completion, "MAX_WAIT_HARD", hard, raising=False)

    # Ensure no stale cancellation from another test leaks in.
    from runtime import _cancel_event
    _cancel_event.clear()

    pane = FakePane(clock, screen_fn)
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell",
                    idle_timeout=idle_timeout)
    screen, method = _wait_polling(info, marker, idle_timeout, max_wait, False)
    return clock, screen, method


MARKER = "___DONE_test_abcd___"


def test_hard_backstop_constant_exists_and_exceeds_soft_ceiling():
    assert getattr(completion, "MAX_WAIT_HARD", None) is not None
    assert completion.MAX_WAIT_HARD > completion.MAX_WAIT


def test_still_changing_past_max_wait_is_not_abandoned(monkeypatch):
    # (a) The pane keeps changing past the soft max_wait, then emits the
    # marker. The loop must NOT bail out with "max_wait" while live — it
    # must keep polling and honor the later marker.
    def screen_fn(elapsed, n):
        if elapsed < 2.0:                      # still busy, repaint each poll
            return [f"compiling unit {n}.o"]
        return [f"EXIT:0 {MARKER}"]            # marker finally appears

    clock, screen, method = _run(
        monkeypatch, screen_fn, marker=MARKER, max_wait=1.0, hard=5.0)

    assert method == "marker"                  # not abandoned at max_wait
    assert clock.elapsed > 1.0                 # we polled past the soft ceiling
    assert clock.elapsed < 5.0                 # and stopped well before hard cap


def test_idle_past_max_wait_returns_max_wait(monkeypatch):
    # (b) With a marker pending (idle strategy disabled), the screen goes
    # idle and the soft ceiling passes -> the command is genuinely stuck,
    # so we DO abandon it with "max_wait" (activity-aware, not hard cap).
    def screen_fn(elapsed, n):
        if elapsed < 0.5:
            return [f"working {n}"]
        return ["stalled — no output"]         # constant -> idle

    clock, screen, method = _run(
        monkeypatch, screen_fn, marker=MARKER, max_wait=1.0,
        hard=5.0, idle_timeout=0.3)

    assert method == "max_wait"
    assert clock.elapsed < 5.0                 # idle-triggered, before hard cap


def test_idle_without_marker_returns_idle(monkeypatch):
    # (b') No marker: the existing idle strategy still fires first and
    # returns "idle" (unchanged behavior — the fix must not break it).
    def screen_fn(elapsed, n):
        if elapsed < 0.5:
            return [f"working {n}"]
        return ["stalled — no output"]

    clock, screen, method = _run(
        monkeypatch, screen_fn, marker=None, max_wait=1.0,
        hard=5.0, idle_timeout=0.3)

    assert method == "idle"
    assert clock.elapsed < 1.0                 # idle fired before max_wait


def test_forever_changing_abandoned_only_after_hard_cap(monkeypatch):
    # (c) A pane that changes forever (never markers, never idles) must be
    # abandoned eventually — but ONLY once the hard backstop is exceeded,
    # never at the soft max_wait.
    def screen_fn(elapsed, n):
        return [f"downloading chunk {n}"]      # always changing

    clock, screen, method = _run(
        monkeypatch, screen_fn, marker=None, max_wait=1.0, hard=5.0)

    assert method == "max_wait"
    assert clock.elapsed >= 5.0                # only after MAX_WAIT_HARD
