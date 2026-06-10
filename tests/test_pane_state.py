"""Tests for visual agent state via tmux pane border coloring (gh#4).

The colorizer adapts the execution `on_event` protocol — see
dag_scheduler.py for the event shapes — and recolors each pane's tmux
border to reflect what the agent is doing in it. Everything is
best-effort: a tmux failure must never propagate into execution.
"""
import pytest
from models import PaneInfo


class _FakePane:
    """Records tmux `.cmd(...)` invocations instead of running them."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def cmd(self, *args):
        if self.fail:
            raise RuntimeError("tmux not attached")
        self.calls.append(args)


def _pane_info(fail=False, name="shell"):
    return PaneInfo(pane=_FakePane(fail=fail), app_type="shell",
                    description="Bash", name=name)


# ─── set_pane_border_color ──────────────────────────────────────────────

def test_set_color_issues_select_pane_with_correct_color():
    from pane_state import set_pane_border_color, PANE_BORDER_COLORS
    pi = _pane_info()
    set_pane_border_color(pi, "working")
    assert pi.pane.calls == [
        ("select-pane", "-P", f"fg={PANE_BORDER_COLORS['working']}")
    ]


def test_set_color_distinct_per_state():
    from pane_state import set_pane_border_color, PANE_BORDER_COLORS
    # Every known state maps to a distinct tmux colour so the four agent
    # states are visually tellable apart.
    assert len(set(PANE_BORDER_COLORS.values())) == len(PANE_BORDER_COLORS)
    for state in PANE_BORDER_COLORS:
        pi = _pane_info()
        set_pane_border_color(pi, state)
        assert pi.pane.calls[0][2] == f"fg={PANE_BORDER_COLORS[state]}"


def test_set_color_unknown_state_is_noop():
    from pane_state import set_pane_border_color
    pi = _pane_info()
    set_pane_border_color(pi, "no-such-state")
    assert pi.pane.calls == []


def test_set_color_none_pane_is_noop():
    from pane_state import set_pane_border_color
    set_pane_border_color(None, "working")  # must not raise


def test_set_color_swallows_tmux_errors():
    from pane_state import set_pane_border_color
    pi = _pane_info(fail=True)
    # tmux raising (e.g. session gone) must not propagate.
    set_pane_border_color(pi, "failed")


# ─── PaneBorderColorizer (on_event adapter) ─────────────────────────────

def test_colorizer_colors_working_on_subtask_start():
    from pane_state import PaneBorderColorizer, PANE_BORDER_COLORS
    panes = {"shell": _pane_info(name="shell")}
    cz = PaneBorderColorizer(panes)
    cz("subtask_start", "s1", "shell", "do the thing")
    assert panes["shell"].pane.calls[-1][2] == f"fg={PANE_BORDER_COLORS['working']}"


def test_colorizer_resolves_pane_for_later_events_via_start():
    from pane_state import PaneBorderColorizer, PANE_BORDER_COLORS
    panes = {"shell": _pane_info(name="shell")}
    cz = PaneBorderColorizer(panes)
    # subtask_start seeds the id->pane map; subtask_done carries only the id.
    cz("subtask_start", "s1", "shell", "desc")
    cz("subtask_done", "s1", "all good", 1.2)
    assert panes["shell"].pane.calls[-1][2] == f"fg={PANE_BORDER_COLORS['done']}"


def test_colorizer_colors_failed_and_idle():
    from pane_state import PaneBorderColorizer, PANE_BORDER_COLORS
    panes = {"a": _pane_info(name="a"), "b": _pane_info(name="b")}
    cz = PaneBorderColorizer(panes)
    cz("subtask_start", "s1", "a", "desc")
    cz("subtask_fail", "s1", "boom")
    assert panes["a"].pane.calls[-1][2] == f"fg={PANE_BORDER_COLORS['failed']}"

    cz("subtask_start", "s2", "b", "desc")
    cz("subtask_skip", "s2", "dependency failed")
    assert panes["b"].pane.calls[-1][2] == f"fg={PANE_BORDER_COLORS['idle']}"


def test_colorizer_turn_event_keeps_working():
    from pane_state import PaneBorderColorizer, PANE_BORDER_COLORS
    panes = {"shell": _pane_info(name="shell")}
    cz = PaneBorderColorizer(panes)
    cz("subtask_start", "s1", "shell", "desc")
    panes["shell"].pane.calls.clear()
    cz("turn", "s1", 2, "thinking")
    assert panes["shell"].pane.calls[-1][2] == f"fg={PANE_BORDER_COLORS['working']}"


def test_colorizer_ignores_unknown_event_kinds():
    from pane_state import PaneBorderColorizer
    panes = {"shell": _pane_info(name="shell")}
    cz = PaneBorderColorizer(panes)
    cz("subtask_start", "s1", "shell", "desc")
    panes["shell"].pane.calls.clear()
    cz("tokens", "s1", 100, 50)  # not a state-changing event
    assert panes["shell"].pane.calls == []


def test_colorizer_unknown_subtask_id_is_noop():
    from pane_state import PaneBorderColorizer
    panes = {"shell": _pane_info(name="shell")}
    cz = PaneBorderColorizer(panes)
    cz("subtask_done", "never-seen", "summary", 1.0)  # must not raise
    assert panes["shell"].pane.calls == []


def test_colorizer_missing_pane_in_registry_is_noop():
    from pane_state import PaneBorderColorizer
    cz = PaneBorderColorizer({})  # empty registry
    cz("subtask_start", "s1", "ghost-pane", "desc")  # must not raise


def test_colorizer_empty_args_is_noop():
    from pane_state import PaneBorderColorizer
    cz = PaneBorderColorizer({"shell": _pane_info()})
    cz("subtask_done")  # malformed event, no args — must not raise


# ─── chain_on_event compose helper ──────────────────────────────────────

def test_chain_invokes_all_callbacks_in_order():
    from pane_state import chain_on_event
    seen = []
    chained = chain_on_event(
        lambda *a: seen.append(("first", a)),
        lambda *a: seen.append(("second", a)),
    )
    chained("subtask_done", "s1", "ok", 1.0)
    assert seen == [
        ("first", ("subtask_done", "s1", "ok", 1.0)),
        ("second", ("subtask_done", "s1", "ok", 1.0)),
    ]


def test_chain_skips_none_callbacks():
    from pane_state import chain_on_event
    seen = []
    chained = chain_on_event(None, lambda *a: seen.append(a), None)
    chained("turn", "s1", 1, "x")
    assert seen == [("turn", "s1", 1, "x")]


def test_chain_of_all_none_returns_none():
    from pane_state import chain_on_event
    assert chain_on_event(None, None) is None


def test_chain_isolates_callback_failure():
    from pane_state import chain_on_event
    seen = []
    chained = chain_on_event(
        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
        lambda *a: seen.append(a),
    )
    chained("subtask_fail", "s1", "err")  # first raises, second must still run
    assert seen == [("subtask_fail", "s1", "err")]
