"""Tests for auto-explore-on-unknown-tool (Phase 4.2, gh#41 Phase 1).

When a planner emits subtask.tools=["ripgrep"] but the registry has no
Tier-2 card for "ripgrep", and CLIVE_AUTO_EXPLORE=1 is set, the worker
context builder should queue a background exploration. The new driver
lands in drivers/.unreviewed/<tool>.md per the gh#41 quarantine; the
current subtask runs without it (load_driver bypasses .unreviewed/);
operator must `clive --promote-driver <tool>` to activate for future
sessions.

Scope (minimum viable per Phase 4 sequencing): helper + one wire-up.
COMMANDS auto-registration (so the new tool is reachable through
`clive-tools list <category>`) is a separate follow-up card.
"""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_auto_explore_cache():
    """The helper memoizes attempts process-locally; clear before each test
    so state from a previous test doesn't mask a re-attempt assertion.
    """
    from discovery.auto import _attempted_explorations
    _attempted_explorations.clear()
    yield
    _attempted_explorations.clear()


# --- is_auto_explore_enabled ---

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CLIVE_AUTO_EXPLORE", raising=False)
    from discovery.auto import is_auto_explore_enabled
    assert is_auto_explore_enabled() is False


def test_enabled_by_env_var(monkeypatch):
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from discovery.auto import is_auto_explore_enabled
    assert is_auto_explore_enabled() is True


@pytest.mark.parametrize("val", ["0", "", "true", "yes", "on"])
def test_only_literal_1_enables(monkeypatch, val):
    """Strict opt-in — only the literal '1' counts. Avoids accidental
    enable from operators who set =true thinking it's a boolean flag.
    """
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", val)
    from discovery.auto import is_auto_explore_enabled
    assert is_auto_explore_enabled() is False


# --- auto_explore_unknown_tool ---

def test_disabled_returns_false_without_calling_discovery(monkeypatch):
    monkeypatch.delenv("CLIVE_AUTO_EXPLORE", raising=False)
    from discovery import auto

    with patch.object(auto, "_explore_async") as mock_explore:
        result = auto.auto_explore_unknown_tool("ripgrep")

    assert result is False
    mock_explore.assert_not_called()


def test_enabled_spawns_background_thread(monkeypatch):
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from discovery import auto

    with patch.object(auto, "_explore_async") as mock_explore:
        result = auto.auto_explore_unknown_tool("ripgrep")

    assert result is True
    # The thread target is _explore_async with (tool_name, drivers_dir).
    # We assert it was called eventually (the thread may have run already).
    # Wait briefly for the daemon thread to start the call.
    import time
    for _ in range(50):
        if mock_explore.called:
            break
        time.sleep(0.01)
    mock_explore.assert_called_once_with("ripgrep", None)


def test_dedup_does_not_re_explore_same_tool(monkeypatch):
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from discovery import auto

    with patch.object(auto, "_explore_async") as mock_explore:
        first = auto.auto_explore_unknown_tool("ripgrep")
        second = auto.auto_explore_unknown_tool("ripgrep")

    assert first is True
    assert second is False  # second call sees the cached attempt
    # Wait for thread to start
    import time
    for _ in range(50):
        if mock_explore.called:
            break
        time.sleep(0.01)
    assert mock_explore.call_count == 1


def test_explore_async_failure_is_swallowed(monkeypatch, caplog):
    """A failed background exploration must not raise into the caller —
    auto-explore is a best-effort side-effect on the operator's behalf.
    """
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from discovery import auto

    def raise_(*a, **kw):
        raise RuntimeError("synthesized failure")

    with patch.object(auto, "explore_tool", side_effect=raise_):
        # _explore_async runs in a thread; calling it directly is fine for
        # the assertion that exceptions are caught and logged, not raised.
        auto._explore_async("ripgrep", None)
    # No exception escaped. We don't assert on caplog content (debug-only
    # logging shape varies); the test is the absence of a thrown error.


# --- wire-up in build_worker_tool_context ---

class _SubtaskStub:
    """Minimal Subtask shape for the wire-up test — avoids constructing a
    full Subtask just to read its .tools attribute.
    """
    def __init__(self, tools):
        self.tools = tools


def test_worker_context_does_not_auto_explore_when_disabled(monkeypatch):
    """The wire-up must respect the env-gate symmetrically with the helper."""
    monkeypatch.delenv("CLIVE_AUTO_EXPLORE", raising=False)
    from prompts import build_worker_tool_context
    from discovery import auto

    with patch.object(auto, "_explore_async") as mock_explore:
        # "ripgrep" intentionally has no Tier-2 card.
        build_worker_tool_context(_SubtaskStub(tools=["ripgrep"]))

    mock_explore.assert_not_called()


def test_worker_context_auto_explores_unknown_when_enabled(monkeypatch):
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from prompts import build_worker_tool_context
    from discovery import auto

    with patch.object(auto, "_explore_async") as mock_explore:
        build_worker_tool_context(_SubtaskStub(tools=["ripgrep"]))

    # Wait for the background thread to start the call.
    import time
    for _ in range(50):
        if mock_explore.called:
            break
        time.sleep(0.01)
    mock_explore.assert_called_once_with("ripgrep", None)


def test_worker_context_does_not_auto_explore_known_tools(monkeypatch):
    """Tools with an existing Tier-2 card must not be re-explored."""
    monkeypatch.setenv("CLIVE_AUTO_EXPLORE", "1")
    from prompts import build_worker_tool_context
    from discovery import auto

    # "jq" is a known COMMAND in the registry — has a Tier-2 card.
    with patch.object(auto, "_explore_async") as mock_explore:
        build_worker_tool_context(_SubtaskStub(tools=["jq"]))

    mock_explore.assert_not_called()
