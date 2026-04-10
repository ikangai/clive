#!/usr/bin/env python3
"""
clive TUI -- Terminal UI for the CLI Live Environment.

Single-screen interface: scrolling output, input line at the bottom.
Configuration via slash commands, everything else is a task.

Slash commands:
    /profile <name>     Switch toolset profile
    /profile +<cat>     Add category to current profile
    /tools              Show available and missing tools
    /install            Install missing CLI tools
    /help               Show this help

Usage:
    python tui.py
    python clive.py --tui
"""

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import llm
from dotenv import load_dotenv

load_dotenv()

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.design import ColorSystem
from textual.widgets import (
    Footer,
    Input,
    RichLog,
    Static,
)

import commands
from dashboard import render_lines
from executor import execute_plan
from session_store import dispatch_session_slash
from llm import PROVIDERS as LLM_PROVIDERS, chat, get_client
from models import SubtaskStatus
from planner import create_plan
from prompts import build_summarizer_prompt, build_triage_prompt
from session import (
    SOCKET_NAME, check_health, generate_session_id, setup_session,
)
from toolsets import (
    PROFILES,
    CATEGORIES,
    DEFAULT_TOOLSET,
    resolve_toolset,
    check_commands,
    build_tools_summary,
)
from tui_theme import LOGO, CLIVE_THEME, CSS
from tui_helpers import build_clive_context, show_tools
from tui_task_runner import run_task_inner
from tui_actions import (
    install_missing, do_install, execute_selfmod, run_selfmod,
    undo_selfmod, run_evolve,
)


# ── Core command handlers (registered via commands registry) ────────────────
#
# Each handler takes (app, arg, out). Registering them in a flat list at
# module load time replaces the if/elif dispatch ladder that used to live in
# CliveApp._handle_command. Adding a new command = one list entry, not two
# sites (dispatch + HELP_TEXT).


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


def _make_session_handler(name: str):
    """Build a (app, arg, out) handler that proxies to dispatch_session_slash.

    Rebuilds the full task string (``"/name arg"``), calls the pure session
    dispatcher, writes the returned lines, and stores the new active session
    id on ``app._active_sid``. This wires the previously-orphaned session
    commands into the TUI without duplicating their logic.
    """
    def _handler(app, arg, out) -> None:
        task = f"{name} {arg}".strip() if arg else name
        _, lines, new_sid = dispatch_session_slash(task, app._active_sid)
        for line in lines:
            out.write(line)
        app._active_sid = new_sid
    _handler.__name__ = f"_cmd_session_{name.lstrip('/').replace('-', '_')}"
    return _handler


def _register_session_commands() -> None:
    """Register the session slash commands (formerly orphaned).

    Previously these lived in session_store.dispatch_session_slash but
    weren't reachable from any entry point — only from unit tests. They're
    now first-class TUI commands via the registry, with ``source="session"``.
    """
    session_cmds = [
        commands.SlashCommand("/sessions", "List all sessions",                 "",             _make_session_handler("/sessions"), source="session"),
        commands.SlashCommand("/new",      "Create and switch to a new session", "[title]",     _make_session_handler("/new"),      source="session"),
        commands.SlashCommand("/resume",   "Switch to an existing session",      "<id>",        _make_session_handler("/resume"),   source="session"),
        commands.SlashCommand("/title",    "Rename the current session",         "<new title>", _make_session_handler("/title"),    source="session"),
        commands.SlashCommand("/session",  "Show the current session id",        "",            _make_session_handler("/session"),  source="session"),
        commands.SlashCommand("/id",       "Show the current session id (alias)","",            _make_session_handler("/id"),       source="session"),
    ]
    for c in session_cmds:
        commands.register(c)


def _prefix_filter(choices):
    """Return a completer that filters ``choices`` by prefix (case-insensitive)."""
    def _complete(arg_prefix: str) -> list[str]:
        if not arg_prefix:
            return list(choices)
        p = arg_prefix.lower()
        return [c for c in choices if c.lower().startswith(p)]
    return _complete


def _register_core_commands() -> None:
    """Register all built-in slash commands. Called once at module load.

    Keeping the list flat and declarative is the whole point — every
    command lives here and only here. Adding "/history" means appending
    one SlashCommand(...) entry, not editing both HELP_TEXT and dispatch.
    """
    # Argument completers — drive a future autocomplete popup. Kept as
    # closures so changes to PROFILES/LLM_PROVIDERS reflect immediately.
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
    for c in core:
        commands.register(c)


_register_core_commands()
_register_session_commands()

# File-based plugin discovery — any *.py file in ~/.clive/commands/ can
# call commands.register() at import time and appear as a first-class
# slash command. Mirrors the skills.py discovery pattern.
_PLUGIN_DIR = os.path.expanduser("~/.clive/commands")
commands.load_plugin_commands(_PLUGIN_DIR)


# ── App ──────────────────────────────────────────────────────────────────────


class CliveApp(App):
    """Clive TUI application."""

    TITLE = "clive"
    CSS = CSS

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._spec = DEFAULT_TOOLSET
        self._resolved = None
        self._available_cmds = []
        self._missing_cmds = []
        self._cancelled = threading.Event()
        # Active tasks: list of {desc, start, pt, ct}
        self._tasks: list[dict] = []
        self._tasks_lock = threading.Lock()
        self._timer = None
        # Pending clarification: {original_task, context}
        self._pending: dict | None = None
        # Active REPL session id (used by /sessions, /new, /resume, /title, /session)
        self._active_sid: str | None = None

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **CLIVE_THEME.generate()}

    def compose(self) -> ComposeResult:
        yield RichLog(id="output", wrap=True, markup=True)
        with Horizontal(id="prompt-row"):
            yield Static("❯", id="prompt-char")
            yield Input(id="prompt-input", placeholder="Enter a task or /help")
        yield Static(f"[#6b7280]profile[/] {self._spec}", id="status-bar")

    def on_mount(self) -> None:
        out = self.query_one("#output", RichLog)
        out.write(LOGO)
        out.write("")
        out.write(
            "[#6b7280]CLI Live Environment · type a task or /help[/]"
        )
        out.write("")
        self._resolve_profile()
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt-input":
            return
        text = event.input.value.strip()
        event.input.value = ""
        if not text:
            return
        # Restore the normal status bar after the user submits — the hint
        # only lives while the user is mid-typing a slash command.
        self._update_status()
        self._handle_input(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live discovery hint: show matching commands while typing /..."""
        if event.input.id != "prompt-input":
            return
        text = event.value
        if not text.startswith("/"):
            # Not a slash command — restore normal status
            self._update_status()
            return
        # Split into (name, arg)
        parts = text.split(None, 1)
        name = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        hint = self._build_slash_hint(name, arg, typing_arg=len(parts) > 1 or text.endswith(" "))
        if hint:
            self.query_one("#status-bar", Static).update(hint)
        else:
            self._update_status()

    def _build_slash_hint(self, name: str, arg: str, typing_arg: bool) -> str:
        """Thin wrapper around commands.build_slash_hint() — kept as an App
        method so the Input.Changed callback has a natural dispatch point,
        but the logic lives in commands.py where it's independently testable."""
        return commands.build_slash_hint(name, arg, typing_arg)

    def _handle_input(self, text: str) -> None:
        out = self.query_one("#output", RichLog)

        # Echo input
        out.write(f"[#d97706]❯[/] {text}")

        if text.startswith("/"):
            self._handle_command(text)
        elif self._pending:
            # User is answering a clarification question
            pending = self._pending
            self._pending = None
            combined = f"{pending['task']}\n\nAdditional context: {text}"
            self._run_task(combined)
        else:
            self._run_task(text)

    def _handle_profile(self, arg: str, out: RichLog) -> None:
        if not arg:
            out.write(f"[#6b7280]Current:[/] {self._spec}")
            out.write(f"[#6b7280]Profiles:[/] {', '.join(PROFILES)}")
            out.write(f"[#6b7280]Categories:[/] {', '.join(sorted(CATEGORIES))}")
            return
        new_spec = self._spec + arg if arg.startswith("+") else arg
        try:
            resolve_toolset(new_spec)
            self._spec = new_spec
            self._resolve_profile()
            out.write(f"[#22c55e]✓[/] Profile: [#c9c9d6]{self._spec}[/]")
        except ValueError as e:
            out.write(f"[#ef4444]✗[/] {e}")

    def _handle_status(self, out: RichLog) -> None:
        with self._tasks_lock:
            tasks = list(self._tasks)
        if not tasks:
            out.write("[#6b7280]No tasks running.[/]")
            return
        for t in tasks:
            elapsed = time.time() - t["start"]
            total = t["pt"] + t["ct"]
            out.write(
                f"[#d97706]●[/] {t['desc'][:70]}  "
                f"[#6b7280]{elapsed:.0f}s · {total:,} tokens[/]"
            )

    def _handle_cancel(self, out: RichLog) -> None:
        with self._tasks_lock:
            n = len(self._tasks)
            self._tasks.clear()
        if not n:
            out.write("[#6b7280]No tasks running.[/]")
            return
        self._cancelled.set()
        try:
            subprocess.run(
                ["tmux", "-L", SOCKET_NAME, "kill-session", "-t", "clive"],
                capture_output=True,
            )
        except Exception:
            pass
        out.write(f"[#f59e0b]Cancelled {n} task(s).[/]")
        self._update_status()

    def _handle_command(self, text: str) -> None:
        out = self.query_one("#output", RichLog)
        parts = text.split(None, 1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Bare "/" → list available commands inline (quick discovery)
        if name == "/":
            for line in commands.format_command_list():
                out.write(line)
            return

        if commands.dispatch(name, arg, self, out):
            return

        # Unknown command — offer a "did you mean" suggestion if we can find one
        suggestions = commands.suggest(name)
        if suggestions:
            hint = ", ".join(suggestions[:3])
            out.write(f"[#ef4444]Unknown command: {name}[/] — did you mean {hint}?")
        else:
            out.write(f"[#ef4444]Unknown command: {name}[/] — try /help")

    def _resolve_profile(self) -> None:
        try:
            self._resolved = resolve_toolset(self._spec)
            self._available_cmds, self._missing_cmds = check_commands(
                self._resolved["commands"]
            )
        except ValueError:
            pass
        self._update_status()

    def _handle_provider(self, arg: str, out: RichLog) -> None:

        if not arg:
            out.write(f"[#6b7280]Current:[/] {llm.PROVIDER_NAME} ({llm.MODEL})")
            out.write(
                f"[#6b7280]Available:[/] {', '.join(LLM_PROVIDERS)}"
            )
            return

        if arg not in LLM_PROVIDERS:
            out.write(
                f"[#ef4444]✗ Unknown provider: {arg}[/]  "
                f"[#6b7280]Available: {', '.join(LLM_PROVIDERS)}[/]"
            )
            return

        provider = LLM_PROVIDERS[arg]
        llm.PROVIDER_NAME = arg
        llm._provider = provider
        llm.MODEL = provider["default_model"]
        os.environ["LLM_PROVIDER"] = arg

        # Set API key env var if needed
        key_env = provider.get("api_key_env")
        if key_env and not os.environ.get(key_env):
            out.write(
                f"[#f59e0b]⚠[/] Set {key_env} in .env or environment"
            )

        out.write(
            f"[#22c55e]✓[/] Provider: [#c9c9d6]{arg}[/]  "
            f"Model: [#c9c9d6]{llm.MODEL}[/]"
        )
        self._update_status()

    def _handle_model(self, arg: str, out: RichLog) -> None:

        if not arg:
            out.write(f"[#6b7280]Current:[/] {llm.MODEL} ({llm.PROVIDER_NAME})")
            return

        llm.MODEL = arg
        os.environ["AGENT_MODEL"] = arg
        out.write(
            f"[#22c55e]✓[/] Model: [#c9c9d6]{arg}[/]  "
            f"Provider: [#c9c9d6]{llm.PROVIDER_NAME}[/]"
        )
        self._update_status()

    @work(thread=True)
    def _do_install(self, brew_pkgs: list, pip_pkgs: list) -> None:
        do_install(self, brew_pkgs, pip_pkgs)

    @work(thread=True)
    def _execute_selfmod(self, goal: str) -> None:
        execute_selfmod(self, goal)

    # ── Task execution ───────────────────────────────────────────────────

    def _run_task(self, task: str) -> None:
        self._cancelled.clear()
        task_info = {"desc": task, "start": time.time(), "pt": 0, "ct": 0}
        with self._tasks_lock:
            self._tasks.append(task_info)
        # Start status timer if first task
        if not self._timer:
            self._timer = self.set_interval(1.0, self._update_status)
        self._execute_task(task, task_info)

    def _finish_task(self, task_info: dict) -> None:
        with self._tasks_lock:
            if task_info in self._tasks:
                self._tasks.remove(task_info)
            has_tasks = bool(self._tasks)
        if not has_tasks and self._timer:
            self._timer.stop()
            self._timer = None
        self._update_status()

    def _update_status(self) -> None:
        parts = [
            f"[#6b7280]profile[/] {self._spec}",
            f"[#6b7280]llm[/] {llm.PROVIDER_NAME}/{llm.MODEL}",
        ]
        with self._tasks_lock:
            n = len(self._tasks)
            total_pt = sum(t["pt"] for t in self._tasks)
            total_ct = sum(t["ct"] for t in self._tasks)
        if n:
            parts.append(f"[#d97706]{n} task{'s' if n > 1 else ''}[/]")
            total = total_pt + total_ct
            if total:
                parts.append(f"[#6b7280]tokens[/] {total:,}")
        self.query_one("#status-bar", Static).update("  ".join(parts))

    def _on_event(self, event_type: str, task_info: dict, *args) -> None:
        if self._cancelled.is_set():
            return
        # Handle token updates directly (thread-safe dict mutation)
        if event_type == "tokens":
            _, pt, ct = args
            task_info["pt"] += pt
            task_info["ct"] += ct
            return
        self.call_from_thread(self._handle_event, event_type, *args)

    def _handle_event(self, event_type: str, *args) -> None:
        out = self.query_one("#output", RichLog)

        if event_type == "subtask_start":
            sid, pane, desc = args
            out.write(f"  [#f59e0b]◐[/] [#c9c9d6]{sid}[/] [#6b7280]{pane}[/] {desc[:70]}")

        elif event_type == "subtask_done":
            sid, summary, elapsed = args
            out.write(
                f"  [#22c55e]✓[/] [#c9c9d6]{sid}[/] {summary[:65]} [#3a3a4a]{elapsed:.1f}s[/]"
            )

        elif event_type == "subtask_fail":
            sid, error = args
            out.write(f"  [#ef4444]✗[/] [#c9c9d6]{sid}[/] {error[:70]}")

        elif event_type == "subtask_skip":
            sid, reason = args
            out.write(f"  [#3a3a4a]– {sid} {reason[:70]}[/]")

        elif event_type == "turn":
            sid, turn_num, cmd_snippet = args
            out.write(
                f"    [#3a3a4a]{sid} t{turn_num}[/] {cmd_snippet[:70]}"
            )

        elif event_type == "tokens":
            pass  # handled directly in _on_event

    @work(thread=True)
    def _execute_task(self, task: str, task_info: dict) -> None:
        out = self.query_one("#output", RichLog)

        # Redirect stdout/stderr — Textual replaces sys.stdout with a
        # capture object that lacks .encoding, breaking libtmux and others.
        devnull = open(os.devnull, "w")
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = devnull
        sys.stderr = devnull

        try:
            run_task_inner(self, task, task_info, out)
        except Exception as e:
            self.call_from_thread(
                out.write, f"[#ef4444]✗ Unexpected error: {e}[/]"
            )
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            devnull.close()
            self.call_from_thread(self._finish_task, task_info)


if __name__ == "__main__":
    app = CliveApp()
    app.run()
