"""Slash command registry — single source of truth for TUI slash commands.

Replaces the if/elif dispatch ladder in tui.py with a declarative registry.
Commands are registered via `register()` and dispatched via `dispatch()`.
HELP_TEXT rendering (a future iteration) will also read from this registry,
eliminating the parallel source-of-truth problem between dispatch and help.

A ``SlashCommand`` is the minimum viable record: name, summary, arg hint,
handler callable, source label. Handlers take ``(app, arg, out)`` — ``app``
is the CliveApp instance (for access to state/helpers), ``arg`` is the text
after the command name, ``out`` is the RichLog widget.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass
class SlashCommand:
    name: str                               # e.g. "/profile" (leading slash included)
    summary: str                            # one-line help description
    args_hint: str = ""                     # optional arg syntax hint e.g. "<name|+cat>"
    handler: Callable = lambda app, arg, out: None
    source: str = "core"                    # "core", "session", "plugin:..."


_REGISTRY: dict[str, SlashCommand] = {}


def register(cmd: SlashCommand) -> None:
    """Add or replace a command in the registry (keyed by name)."""
    _REGISTRY[cmd.name] = cmd


def get(name: str) -> SlashCommand | None:
    """Look up a command by name. Returns None if not registered."""
    return _REGISTRY.get(name)


def all_commands() -> list[SlashCommand]:
    """Return all registered commands in registration order."""
    return list(_REGISTRY.values())


def names() -> list[str]:
    """Return all registered command names."""
    return list(_REGISTRY.keys())


def dispatch(name: str, arg: str, app, out) -> bool:
    """Look up and invoke a command. Returns True if handled, False if unknown.

    The TUI falls back to its "Unknown command" message when this returns False.
    """
    cmd = _REGISTRY.get(name)
    if cmd is None:
        return False
    cmd.handler(app, arg, out)
    return True


def clear() -> None:
    """Reset the registry. Test helper only."""
    _REGISTRY.clear()
