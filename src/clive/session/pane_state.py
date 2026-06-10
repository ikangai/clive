"""Visual agent state via tmux pane border coloring (gh#4).

Pure UX. When a human is attached to the clive tmux session, each pane's
border color signals what the agent is doing in it:

    working (yellow) · done (green) · failed (red) · idle/skipped (grey)

The colorizer adapts the execution `on_event` callback protocol
(see ``planning/dag_scheduler.py`` for event shapes) into per-pane
``select-pane -P`` style updates. ``subtask_start`` carries the pane name
and seeds a subtask-id → pane mapping; later events carry only the id and
are resolved through that mapping.

Everything here is best-effort: every tmux call is wrapped so a coloring
failure (session gone, not attached, old tmux) never propagates into
execution. No-op-safe when the pane registry can't resolve a target.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# tmux 256-color palette codes, one per agent state. Kept distinct so the
# four states are visually tellable apart on the pane border.
PANE_BORDER_COLORS = {
    "working": "colour3",  # yellow — agent is thinking/typing in this pane
    "done":    "colour2",  # green  — subtask completed successfully
    "failed":  "colour1",  # red    — subtask failed
    "idle":    "colour8",  # grey   — skipped / no longer active
}

# Map execution event kinds to a visual state. Events not listed here
# (e.g. "tokens") don't change the border. See dag_scheduler.py / the
# runners for the emitting sites.
EVENT_TO_STATE = {
    "subtask_start": "working",
    "turn":          "working",
    "probe":         "working",
    "squash":        "working",
    "subtask_done":  "done",
    "subtask_fail":  "failed",
    "subtask_skip":  "idle",
}


def set_pane_border_color(pane_info, state: str) -> None:
    """Set a pane's tmux border style to the color for ``state``.

    Best-effort and side-effect-only: unknown state, a ``None`` pane, or a
    tmux error are all silently no-ops so a UX nicety can never disrupt the
    run.
    """
    color = PANE_BORDER_COLORS.get(state)
    if color is None or pane_info is None:
        return
    try:
        pane_info.pane.cmd("select-pane", "-P", f"fg={color}")
    except Exception:
        log.debug("pane border color update failed (state=%s)", state, exc_info=True)


class PaneBorderColorizer:
    """``on_event``-compatible callable that recolors pane borders by state.

    Construct with the session's ``panes`` registry (``name -> PaneInfo``)
    and pass the instance anywhere an ``on_event(event_type, *args)``
    callback is accepted. It maintains its own subtask-id → pane-name map,
    seeded from ``subtask_start`` events.
    """

    def __init__(self, panes: dict):
        self.panes = panes
        self._id_to_pane: dict[str, str] = {}

    def __call__(self, event_type, *args) -> None:
        state = EVENT_TO_STATE.get(event_type)
        if state is None or not args:
            return
        sid = args[0]
        # subtask_start is the only event carrying the pane name; record it.
        if event_type == "subtask_start" and len(args) >= 2:
            self._id_to_pane[sid] = args[1]
        pane_name = self._id_to_pane.get(sid)
        if pane_name is None:
            return
        set_pane_border_color(self.panes.get(pane_name), state)


def chain_on_event(*callbacks):
    """Compose several ``on_event`` callbacks into one.

    ``None`` callbacks are dropped; if none remain, returns ``None`` so the
    caller's existing "no callback" fast path is preserved. Each callback is
    isolated — one raising does not stop the others.
    """
    cbs = [c for c in callbacks if c is not None]
    if not cbs:
        return None
    if len(cbs) == 1:
        return cbs[0]

    def _chained(*args):
        for cb in cbs:
            try:
                cb(*args)
            except Exception:
                log.debug("chained on_event callback failed", exc_info=True)

    return _chained
