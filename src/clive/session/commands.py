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
    complete: Callable[[str], list[str]] | None = None
    """Optional argument completer. Takes the current arg prefix and returns
    candidate completions. Used by future interactive discovery UI
    (Claude-Code-style popup). ``None`` means "no completions for this arg"."""


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


def complete_command_name(prefix: str) -> list[str]:
    """Return command names that start with ``prefix``. Case-insensitive.

    Feeds a future autocomplete popup: when the user types ``/pr``, return
    ``["/profile", "/provider"]``.
    """
    if not prefix:
        return list(_REGISTRY.keys())
    prefix_lower = prefix.lower()
    return [name for name in _REGISTRY if name.lower().startswith(prefix_lower)]


def complete_arg(name: str, arg_prefix: str) -> list[str]:
    """Return argument completions for ``name`` given current arg prefix.

    Returns ``[]`` if the command is unknown or has no registered completer.
    """
    cmd = _REGISTRY.get(name)
    if cmd is None or cmd.complete is None:
        return []
    return cmd.complete(arg_prefix)


def format_command_list() -> list[str]:
    """Return a compact inline listing of every registered command.

    One line per command, grouped by source. Used when the user types a
    bare ``/`` in the TUI to get quick discovery without the full help block.
    """
    lines: list[str] = []
    entries = all_commands()
    if not entries:
        return ["[#6b7280]No commands registered.[/]"]

    # Group by source (core first, then session, then anything else)
    order = {"core": 0, "session": 1}
    entries = sorted(entries, key=lambda c: (order.get(c.source, 99), c.name))

    name_width = max(len(c.name) for c in entries) + 2
    last_source: str | None = None
    for c in entries:
        if c.source != last_source:
            if last_source is not None:
                lines.append("")
            lines.append(f"[#d97706]{c.source}[/]")
            last_source = c.source
        pad = " " * (name_width - len(c.name))
        lines.append(f"  [#c9c9d6]{c.name}[/]{pad}{c.summary}")
    return lines


def build_slash_hint(name: str, arg: str, typing_arg: bool) -> str:
    """Return a Rich-markup status-bar hint for a partial ``/…`` input.

    Called from TUI's ``on_input_changed`` handler. Three modes:

    - Exact match AND user typing an arg → show ``<name> <hint>  → <completions>``
    - Exact match, no arg context → show ``<name> <hint>  <summary>``
    - No exact match → show up to 4 candidate command names

    Returns an empty string when there's nothing useful to display.
    """
    exact = get(name)
    if exact and typing_arg:
        completions = complete_arg(name, arg.strip())[:6]
        if completions:
            return (
                f"[#d97706]{exact.name}[/] [#6b7280]{exact.args_hint}[/]  "
                f"→ {'  '.join(completions)}"
            )
        return f"[#d97706]{exact.name}[/] [#6b7280]{exact.args_hint}[/]  {exact.summary}"
    if exact:
        return f"[#d97706]{exact.name}[/] [#6b7280]{exact.args_hint}[/]  {exact.summary}"
    matches = complete_command_name(name)[:4]
    if matches:
        return "[#6b7280]matches:[/] " + "  ".join(matches)
    return ""


def suggest(unknown_name: str, limit: int = 3) -> list[str]:
    """Return up to ``limit`` registered command names close to ``unknown_name``.

    Uses ``difflib.get_close_matches`` — a simple Levenshtein-ish similarity
    that's good enough for "did you mean" hints on typos like /profil or /help2.
    """
    import difflib
    return difflib.get_close_matches(unknown_name, list(_REGISTRY.keys()), n=limit, cutoff=0.6)


def load_plugin_commands(plugin_dir: str) -> list[str]:
    """Discover and import every ``*.py`` file in ``plugin_dir``.

    Plugin files are expected to import ``commands`` and call ``register()``
    at module load time. This gives ``/selfmod`` (and human maintainers) a
    drop-in plugin surface — adding a new command means writing a file, not
    patching ``tui.py``. The ``skills.py`` module already uses this pattern
    for workflow recipes; this aligns the slash-command surface with it.

    Returns the list of loaded file basenames (for logging). Silently skips
    files that raise on import — a broken plugin must never take down the TUI.
    """
    import importlib.util
    import os

    loaded: list[str] = []
    if not os.path.isdir(plugin_dir):
        return loaded

    for fname in sorted(os.listdir(plugin_dir)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(plugin_dir, fname)
        mod_name = f"_clive_plugin_{fname[:-3]}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            loaded.append(fname)
        except Exception:
            # Broken plugin — skip silently. Plugins are advisory.
            continue
    return loaded


def render_help(profiles: str, categories: str, providers: str) -> str:
    """Render the slash-command help block from the registry.

    Returns a Rich-markup string suitable for ``RichLog.write()``. The
    column widths are computed from the widest registered "name + args"
    so newly registered commands stay aligned without manual tweaking.
    """
    entries = all_commands()
    if not entries:
        return "[#6b7280]No commands registered.[/]"

    # Visible width of "<name> <args_hint>" (no markup characters)
    def visible_len(c: SlashCommand) -> int:
        return len(c.name) + (1 + len(c.args_hint) if c.args_hint else 0)

    col_width = max(visible_len(c) for c in entries) + 2

    lines = ["[#d97706]Slash commands:[/]"]
    for c in entries:
        left_markup = f"[#c9c9d6]{c.name}[/]"
        if c.args_hint:
            left_markup += f" [#6b7280]{c.args_hint}[/]"
        pad = " " * max(2, col_width - visible_len(c))
        lines.append(f"  {left_markup}{pad}{c.summary}")
    lines.append("")
    lines.append(f"[#d97706]Profiles:[/]   {profiles}")
    lines.append(f"[#d97706]Categories:[/]  {categories}")
    lines.append(f"[#d97706]Providers:[/]   {providers}")
    lines.append("")
    lines.append("[#6b7280]Type anything else to run it as a task.[/]")
    return "\n".join(lines)
