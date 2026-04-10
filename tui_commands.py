"""Command handler implementations + registration for the TUI slash registry.

Extracted from tui.py as the final step of the registry refactor. This file
owns:

1. The module-level ``_cmd_*`` handlers — each takes ``(app, arg, out)``.
2. ``_make_session_handler()`` — the proxy that adapts session_store's pure
   ``dispatch_session_slash`` into the registry handler contract.
3. ``register_all()`` — registers the 14 core commands and 6 session
   commands. Called once by tui.py at module load.

Keeping these out of tui.py means adding a new slash command no longer
requires touching the App class file. tui.py is pure orchestration; this
file is pure registration.
"""

import os

import commands
from dashboard import render_lines
from llm import PROVIDERS as LLM_PROVIDERS
from session_store import dispatch_session_slash
from toolsets import PROFILES, CATEGORIES
from tui_actions import install_missing, run_selfmod, undo_selfmod, run_evolve
from tui_helpers import show_tools


# ── Core command handlers ──────────────────────────────────────────────────


def _cmd_help(app, arg, out) -> None:
    out.write(commands.render_help(
        profiles=", ".join(PROFILES),
        categories=", ".join(sorted(CATEGORIES)),
        providers=", ".join(LLM_PROVIDERS),
    ))


def _cmd_profile(app, arg, out) -> None:
    app._handle_profile(arg, out)


def _cmd_status(app, arg, out) -> None:
    app._handle_status(out)


def _cmd_cancel(app, arg, out) -> None:
    app._handle_cancel(out)


def _cmd_clear(app, arg, out) -> None:
    out.clear()


def _cmd_provider(app, arg, out) -> None:
    app._handle_provider(arg, out)


def _cmd_model(app, arg, out) -> None:
    app._handle_model(arg, out)


def _cmd_tools(app, arg, out) -> None:
    show_tools(out, app._resolved, app._available_cmds, app._missing_cmds)


def _cmd_install(app, arg, out) -> None:
    install_missing(app)


def _cmd_selfmod(app, arg, out) -> None:
    if not arg:
        out.write("[#6b7280]Usage: /selfmod <goal>[/]")
        out.write("[#6b7280]Example: /selfmod add a /history command that shows past tasks[/]")
        return
    run_selfmod(app, arg)


def _cmd_undo(app, arg, out) -> None:
    undo_selfmod(app)


def _cmd_safe_mode(app, arg, out) -> None:
    os.environ["CLIVE_EXPERIMENTAL_SELFMOD"] = "0"
    out.write("[#22c55e]✓[/] Self-modification disabled for this session.")


def _cmd_evolve(app, arg, out) -> None:
    if arg:
        out.write(f"[bold]Evolving {arg} driver...[/bold]")
        run_evolve(app, arg, out)
    else:
        out.write("[yellow]Usage: /evolve <driver> (shell, browser, all)[/yellow]")


def _cmd_dashboard(app, arg, out) -> None:
    for line in render_lines():
        out.write(line)


# ── Session command proxy ──────────────────────────────────────────────────


def _make_session_handler(name: str):
    """Build a (app, arg, out) handler that proxies to dispatch_session_slash.

    Rebuilds the full task string, calls the pure session dispatcher, writes
    the returned lines, and stores the new active session id on
    ``app._active_sid``.
    """
    def _handler(app, arg, out) -> None:
        task = f"{name} {arg}".strip() if arg else name
        _, lines, new_sid = dispatch_session_slash(task, app._active_sid)
        for line in lines:
            out.write(line)
        app._active_sid = new_sid
    _handler.__name__ = f"_cmd_session_{name.lstrip('/').replace('-', '_')}"
    return _handler


# ── Arg completers ──────────────────────────────────────────────────────────


def _prefix_filter(choices):
    """Return a completer that filters ``choices`` by prefix (case-insensitive)."""
    def _complete(arg_prefix: str) -> list[str]:
        if not arg_prefix:
            return list(choices)
        p = arg_prefix.lower()
        return [c for c in choices if c.lower().startswith(p)]
    return _complete


# ── Registration ───────────────────────────────────────────────────────────


def register_all() -> None:
    """Register every built-in command. Called once by tui.py at module load."""
    profile_choices = lambda: sorted(PROFILES) + [f"+{c}" for c in sorted(CATEGORIES)]
    provider_choices = lambda: sorted(LLM_PROVIDERS.keys())
    evolve_choices = ["shell", "browser", "all"]

    core = [
        commands.SlashCommand("/help",      "Show this help",                         "",            _cmd_help),
        commands.SlashCommand("/profile",   "Switch toolset profile or add category", "<name|+cat>", _cmd_profile,
                              complete=lambda p: _prefix_filter(profile_choices())(p)),
        commands.SlashCommand("/provider",  "Switch LLM provider",                    "<name>",      _cmd_provider,
                              complete=lambda p: _prefix_filter(provider_choices())(p)),
        commands.SlashCommand("/model",     "Switch model",                           "<name>",      _cmd_model),
        commands.SlashCommand("/tools",     "Show available and missing tools",       "",            _cmd_tools),
        commands.SlashCommand("/install",   "Install missing CLI tools",              "",            _cmd_install),
        commands.SlashCommand("/status",    "Show running task status",               "",            _cmd_status),
        commands.SlashCommand("/cancel",    "Cancel the running task",                "",            _cmd_cancel),
        commands.SlashCommand("/clear",     "Clear the screen",                       "",            _cmd_clear),
        commands.SlashCommand("/selfmod",   "Self-modify clive (experimental)",       "<goal>",      _cmd_selfmod),
        commands.SlashCommand("/undo",      "Roll back last self-modification",       "",            _cmd_undo),
        commands.SlashCommand("/safe-mode", "Disable self-modification",              "",            _cmd_safe_mode),
        commands.SlashCommand("/evolve",    "Evolve a driver (shell, browser, all)",  "<driver>",    _cmd_evolve,
                              complete=_prefix_filter(evolve_choices)),
        commands.SlashCommand("/dashboard", "Show running clive instances",           "",            _cmd_dashboard),
    ]

    session_cmds = [
        commands.SlashCommand("/sessions", "List all sessions",                  "",            _make_session_handler("/sessions"), source="session"),
        commands.SlashCommand("/new",      "Create and switch to a new session", "[title]",     _make_session_handler("/new"),      source="session"),
        commands.SlashCommand("/resume",   "Switch to an existing session",      "<id>",        _make_session_handler("/resume"),   source="session"),
        commands.SlashCommand("/title",    "Rename the current session",         "<new title>", _make_session_handler("/title"),    source="session"),
        commands.SlashCommand("/session",  "Show the current session id",        "",            _make_session_handler("/session"),  source="session"),
        commands.SlashCommand("/id",       "Show the current session id (alias)", "",           _make_session_handler("/id"),       source="session"),
    ]

    for c in core:
        commands.register(c)
    for c in session_cmds:
        commands.register(c)
