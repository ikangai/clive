"""Bounded exploration runner for the self-learning tool discovery system (gh#41).

Adapter over ``execution.interactive_runner.run_subtask_interactive`` — we get
intervention detection, streaming-obs, per-pane locking, and context compression
for free. The explorer-specific additions are:

  - A pre-built ``Subtask(mode="interactive")`` targeting an exploration pane
    whose ``app_type='explore'`` routes through ``drivers/explore.md``.
  - A per-tool session directory under ``session_dir_root`` so concurrent
    explorations don't share a workspace.
  - Exploration-specific safety (``_check_exploration_safety``) layered on top
    of ``_check_command_safety``: credential-prompt tools (aws, gh, ssh, ...) and
    interactive TUIs (vim, less, lazygit, ...) are refused without an explicit
    ``--help`` / ``-h`` / ``--version`` flag.
  - An ``on_event`` callback that captures the ``probe`` events emitted by
    ``run_subtask_interactive`` into ``ProbeOutcome``s on the result.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

import libtmux

from execution.interactive_runner import run_subtask_interactive
from models import PaneInfo, Subtask
from runtime import _check_command_safety, _strip_sudo_and_env
from session import SOCKET_NAME, add_pane, detach_stream

from .generator import _check_tool_name
from .models import ExplorationResult, ProbeOutcome
from .prompts import (
    CREDENTIAL_TOOLS,
    INTERACTIVE_TOOLS,
    build_exploration_goal,
)

log = logging.getLogger(__name__)

_DEFAULT_MAX_TURNS = 8
_HELP_FLAGS = ("--help", "-h", "--version", "-V")


def _check_exploration_safety(command: str, tool_name: str) -> str | None:
    """Run the standard safety check, then layer on exploration-specific rules.

    Returns a violation string or None if the command is acceptable.
    """
    base = _check_command_safety(command)
    if base is not None:
        return base

    cmd_trimmed = command.strip()
    if not cmd_trimmed:
        return None
    tokens = _strip_sudo_and_env(cmd_trimmed.split())
    if not tokens:
        return None
    head = tokens[0]

    has_help_flag = any(flag in tokens for flag in _HELP_FLAGS)

    if head in CREDENTIAL_TOOLS and not has_help_flag:
        return (
            f"Blocked: `{head}` may prompt for credentials. "
            f"Append --help or --version, or skip this probe."
        )
    if head in INTERACTIVE_TOOLS and not has_help_flag:
        return (
            f"Blocked: `{head}` opens an interactive TUI. "
            f"Append --help or skip this probe."
        )
    return None


def explore_tool(
    tool_name: str,
    session_dir_root: str = "/tmp/clive",
    max_turns: int = _DEFAULT_MAX_TURNS,
    pane_info: Optional[PaneInfo] = None,
) -> ExplorationResult:
    """Run a bounded exploration session for ``tool_name``.

    Reuses ``run_subtask_interactive`` for the loop. Probes are captured via
    the ``probe`` event the runner emits per turn. Returns an ``ExplorationResult``.

    Raises ``ValueError`` if ``tool_name`` is unsafe or reserved — checked at
    the top of the function so no LLM tokens are spent and no tmux pane is
    opened for a bad name (gh#41 debug Bug 2).
    """
    _check_tool_name(tool_name)
    sd = os.path.join(session_dir_root, f"explore-{tool_name}-{uuid.uuid4().hex[:6]}")
    os.makedirs(sd, exist_ok=True)

    own_pane = pane_info is None
    pane = pane_info if pane_info is not None else _open_exploration_pane(sd)

    result = ExplorationResult(tool_name=tool_name)

    def on_event(event_type, *args):
        if event_type == "probe":
            _sid, cmd, exit_code, screen = args
            result.probes.append(ProbeOutcome(
                command=cmd, exit_code=exit_code, screen=screen,
            ))

    subtask = Subtask(
        id=f"explore-{tool_name}",
        description=build_exploration_goal(tool_name),
        pane="explore",
        depends_on=[],
        mode="interactive",
        max_turns=max_turns,
    )

    try:
        runner_result = run_subtask_interactive(
            subtask, pane, dep_context="", on_event=on_event, session_dir=sd,
        )
        if runner_result.summary:
            result.summary = runner_result.summary
    finally:
        if own_pane:
            _close_exploration_pane(pane)

    return result


# ─── Pane lifecycle helpers (separated for monkeypatching in tests) ──────


def _open_exploration_pane(session_dir: str) -> PaneInfo:
    """Create a one-off tmux session + exploration pane.

    Uses ``add_pane`` with ``app_type='explore'`` so the standard driver
    loader picks up ``drivers/explore.md``.
    """
    server = libtmux.Server(socket_name=SOCKET_NAME)
    sess_name = f"clive-explore-{os.path.basename(session_dir)}"
    sess = server.new_session(
        session_name=sess_name, attach=False, window_name="explore",
    )
    pane_def = {
        "name": "explore",
        "app_type": "explore",
        "description": "tool exploration pane",
    }
    return add_pane(sess, pane_def, session_dir)


def _close_exploration_pane(pane_info: PaneInfo) -> None:
    try:
        detach_stream(pane_info)
    except Exception:
        log.debug("detach_stream failed during exploration teardown", exc_info=True)
