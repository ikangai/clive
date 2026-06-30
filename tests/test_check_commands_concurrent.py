"""check_commands probes tools concurrently (bounded thread pool).

The per-tool 'check' shell probes are independent and subprocess-bound, so
they run on a bounded ThreadPoolExecutor instead of sequentially. These tests
pin the two invariants that matter:

  - the (available, missing) partition (and its order) is unchanged, and
  - probes genuinely overlap, so wall-time is ~one timeout regardless of count.
"""
import threading

import toolsets


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess — only returncode is read."""

    def __init__(self, returncode):
        self.returncode = returncode


def test_partition_preserved(monkeypatch):
    """A mix of pass / fail / OSError / no-check probes splits exactly as
    the old sequential loop did — same membership AND same order."""
    commands = [
        {"name": "ok1", "check": "probe ok"},
        {"name": "bad1", "check": "probe bad"},
        {"name": "boom1", "check": "probe boom"},
        {"name": "nocheck"},               # no 'check' key  → available
        {"name": "empty", "check": ""},     # empty 'check'   → available
        {"name": "ok2", "check": "probe ok"},
    ]

    def fake_run(check, **kwargs):
        if "boom" in check:
            raise OSError("no such file")          # mirrors a missing binary
        return _FakeCompleted(0 if "ok" in check else 1)

    monkeypatch.setattr(toolsets.subprocess, "run", fake_run)

    available, missing = toolsets.check_commands(commands)

    assert [c["name"] for c in available] == ["ok1", "nocheck", "empty", "ok2"]
    assert [c["name"] for c in missing] == ["bad1", "boom1"]


def test_runs_concurrently():
    """A probe that blocks on a 2-party Barrier returns without deadlock,
    proving at least two probes run in parallel. Under a sequential loop the
    first probe would never be released and the barrier would time out."""
    barrier = threading.Barrier(2)

    def fake_run(check, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            # Only happens if probes run one-at-a-time: the lone waiter times
            # out. Report it as "missing" so the assertion below fails loudly
            # instead of the suite hanging.
            return _FakeCompleted(1)
        return _FakeCompleted(0)

    import unittest.mock as mock
    with mock.patch.object(toolsets.subprocess, "run", fake_run):
        commands = [{"name": "a", "check": "a"}, {"name": "b", "check": "b"}]
        available, missing = toolsets.check_commands(commands)

    assert [c["name"] for c in available] == ["a", "b"]
    assert missing == []
