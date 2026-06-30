"""Retry a flaky auto-exploration instead of trapping the tool forever (gh#41).

``auto_explore_unknown_tool`` adds the tool to ``_attempted_explorations`` before
the worker thread runs, and ``_explore_async`` logs + swallows ANY failure. Left
as-is, a single transient miss (an LLM hiccup, a tmux glitch, a generate_driver
validation ValueError) permanently blocked re-discovery of that tool for the life
of the clive process. The fix releases the tool from the attempted set on failure
so a later subtask can re-queue it, bounded by a small per-tool failure cap so a
genuinely-broken tool can't thrash.

These tests drive purely through the module's monkeypatchable ``explore_tool``
trampoline — no tmux, no network. ``explore_tool`` raises before any of
``generate_driver`` / ``write_generated_driver`` / ``record_tool_memo`` is reached.
"""
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _clear_auto_explore_state():
    """Both the in-flight set and the failure-counter dict are process-local;
    clear them before and after each test so leaked state can't mask an
    assertion about re-queuing.
    """
    from discovery.auto import _attempted_explorations, _explore_attempts
    _attempted_explorations.clear()
    _explore_attempts.clear()
    yield
    _attempted_explorations.clear()
    _explore_attempts.clear()


def _join_explore_threads(tool_name, timeout=5.0):
    """Join the daemon exploration thread(s) spawned for ``tool_name``.

    The thread is named ``auto-explore-<tool>`` (see ``auto_explore_unknown_tool``).
    A thread that already completed is simply absent from ``threading.enumerate()``
    — its failure handler (the release) runs inside ``run()`` before the thread
    leaves the active set, so the post-join state is settled either way.
    """
    for t in list(threading.enumerate()):
        if t.name == f"auto-explore-{tool_name}":
            t.join(timeout)


def _raise(*_a, **_kw):
    raise RuntimeError("synthesized exploration failure")


def test_failed_exploration_is_re_queued_up_to_cap_then_refused(monkeypatch):
    """With explore_tool raising, each failed attempt releases the tool so the
    next call re-queues (returns True) — up to the per-tool cap — and returns
    False once the cap is hit. This is the gh#41 acceptance.
    """
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from discovery import auto

    monkeypatch.setattr(auto, "explore_tool", _raise)

    tool = "ripgrep"
    cap = auto._MAX_EXPLORE_ATTEMPTS

    # Each call queues a real background thread; joining it lets the failure
    # handler release the tool before we probe the next call.
    for attempt in range(cap):
        assert auto.auto_explore_unknown_tool(tool) is True, (
            f"attempt {attempt + 1}/{cap} should re-queue after a flaky miss"
        )
        _join_explore_threads(tool)

    # Cap reached: the last failure left the tool trapped — no more re-queuing.
    assert auto.auto_explore_unknown_tool(tool) is False
    # Still refused on subsequent calls (the trap is sticky once the cap hits).
    assert auto.auto_explore_unknown_tool(tool) is False
    assert auto._explore_attempts[tool] == cap


def test_inflight_exploration_is_not_re_queued(monkeypatch):
    """While an exploration is in-flight (not yet failed/released) a second
    call must not double-queue — the dedup guard is unchanged.
    """
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from discovery import auto

    started = threading.Event()
    release = threading.Event()

    def _block(*_a, **_kw):
        started.set()
        release.wait(5.0)
        raise RuntimeError("synthesized exploration failure")

    monkeypatch.setattr(auto, "explore_tool", _block)

    tool = "fd"
    assert auto.auto_explore_unknown_tool(tool) is True
    assert started.wait(5.0)  # the first exploration is now inside explore_tool

    # Second call while the first is mid-flight: refused (tool still claimed).
    assert auto.auto_explore_unknown_tool(tool) is False

    release.set()
    _join_explore_threads(tool)

    # After the first failure released it, a re-queue is allowed again.
    assert auto.auto_explore_unknown_tool(tool) is True
    _join_explore_threads(tool)


def test_successful_exploration_stays_one_attempt(monkeypatch):
    """The success path is unchanged: a tool explored once is never re-queued,
    and the failure counter is never touched.
    """
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from discovery import auto

    calls = []

    def _ok_explore(tool_name, *_a, **_kw):
        calls.append(tool_name)
        return object()

    monkeypatch.setattr(auto, "explore_tool", _ok_explore)
    monkeypatch.setattr(auto, "generate_driver", lambda *a, **kw: "synopsis line\n")
    monkeypatch.setattr(
        auto, "write_generated_driver", lambda *a, **kw: "/tmp/x.md"
    )
    monkeypatch.setattr(
        "discovery.tool_memo.record_tool_memo", lambda *a, **kw: None
    )

    tool = "jqx"
    assert auto.auto_explore_unknown_tool(tool) is True
    _join_explore_threads(tool)

    # Re-queue refused: a successful exploration leaves the tool claimed.
    assert auto.auto_explore_unknown_tool(tool) is False
    # Give a stray second thread (there should be none) a beat — call count
    # must remain exactly one.
    time.sleep(0.05)
    assert calls == [tool]
    # The failure-counter dict is untouched on the happy path.
    assert tool not in auto._explore_attempts
