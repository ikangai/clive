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

from dashboard import render_lines
from executor import execute_plan
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

LOGO = """\
[#e8915a] ██████╗██╗     ██╗██╗   ██╗███████╗[/]
[#d97706]██╔════╝██║     ██║██║   ██║██╔════╝[/]
[#d97706]██║     ██║     ██║██║   ██║█████╗  [/]
[#c2650a]██║     ██║     ██║╚██╗ ██╔╝██╔══╝  [/]
[#c2650a]╚██████╗███████╗██║ ╚████╔╝ ███████╗[/]
[#b45309] ╚═════╝╚══════╝╚═╝  ╚═══╝  ╚══════╝[/]"""

HELP_TEXT = """\
[#d97706]Slash commands:[/]
  [#c9c9d6]/profile[/] [#6b7280]<name|+cat>[/]  Switch toolset profile or add category
  [#c9c9d6]/provider[/] [#6b7280]<name>[/]      Switch LLM provider
  [#c9c9d6]/model[/] [#6b7280]<name>[/]         Switch model
  [#c9c9d6]/tools[/]                Show available and missing tools
  [#c9c9d6]/install[/]              Install missing CLI tools
  [#c9c9d6]/status[/]               Show running task status
  [#c9c9d6]/cancel[/]               Cancel the running task
  [#c9c9d6]/clear[/]                Clear the screen
  [#c9c9d6]/selfmod[/] [#6b7280]<goal>[/]       Self-modify clive (experimental)
  [#c9c9d6]/undo[/]                 Roll back last self-modification
  [#c9c9d6]/safe-mode[/]            Disable self-modification
  [#c9c9d6]/evolve[/] [#6b7280]<driver>[/]     Evolve a driver (shell, browser, all)
  [#c9c9d6]/dashboard[/]            Show running clive instances
  [#c9c9d6]/help[/]                 Show this help

[#d97706]Profiles:[/]   {profiles}
[#d97706]Categories:[/]  {categories}
[#d97706]Providers:[/]   {providers}

[#6b7280]Type anything else to run it as a task.[/]"""

# ── Theme ───────────────────────────────────────────────────────────────────

CLIVE_THEME = ColorSystem(
    primary="#d97706",
    secondary="#6b7280",
    warning="#f59e0b",
    error="#ef4444",
    success="#22c55e",
    accent="#d97706",
    background="#111118",
    surface="#16161e",
    panel="#1c1c27",
    dark=True,
)

# ── CSS ─────────────────────────────────────────────────────────────────────

CSS = """
Screen {
    background: #111118;
}

#output {
    height: 1fr;
    background: #111118;
    padding: 0 2;
    scrollbar-size: 1 1;
    scrollbar-color: #2a2a3a;
    scrollbar-color-hover: #3a3a4a;
    scrollbar-color-active: #d97706;
}

#prompt-row {
    height: 1;
    padding: 0 2;
    background: #111118;
    margin-bottom: 0;
}

#prompt-char {
    width: 2;
    height: 1;
    color: #d97706;
    background: #111118;
    padding: 0;
}

#prompt-input {
    height: 1;
    background: #111118;
    border: none;
    color: #c9c9d6;
    padding: 0;
    margin: 0;
}

#prompt-input:focus {
    border: none;
}

#status-bar {
    height: 1;
    dock: bottom;
    background: #16161e;
    color: #6b7280;
    padding: 0 2;
}

Footer {
    display: none;
}
"""

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
        self._handle_input(text)

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

    def _handle_command(self, text: str) -> None:
        out = self.query_one("#output", RichLog)
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            out.write(HELP_TEXT.format(
                profiles=", ".join(PROFILES),
                categories=", ".join(sorted(CATEGORIES)),
                providers=", ".join(LLM_PROVIDERS),
            ))

        elif cmd == "/profile":
            if not arg:
                out.write(f"[#6b7280]Current:[/] {self._spec}")
                out.write(
                    f"[#6b7280]Profiles:[/] {', '.join(PROFILES)}"
                )
                out.write(
                    f"[#6b7280]Categories:[/] {', '.join(sorted(CATEGORIES))}"
                )
                return
            if arg.startswith("+"):
                new_spec = self._spec + arg
            else:
                new_spec = arg
            try:
                resolve_toolset(new_spec)
                self._spec = new_spec
                self._resolve_profile()
                out.write(f"[#22c55e]✓[/] Profile: [#c9c9d6]{self._spec}[/]")
            except ValueError as e:
                out.write(f"[#ef4444]✗[/] {e}")

        elif cmd == "/status":
            with self._tasks_lock:
                tasks = list(self._tasks)
            if tasks:
                for t in tasks:
                    elapsed = time.time() - t["start"]
                    total = t["pt"] + t["ct"]
                    out.write(
                        f"[#d97706]●[/] {t['desc'][:70]}  "
                        f"[#6b7280]{elapsed:.0f}s · {total:,} tokens[/]"
                    )
            else:
                out.write("[#6b7280]No tasks running.[/]")

        elif cmd == "/cancel":
            with self._tasks_lock:
                n = len(self._tasks)
                self._tasks.clear()
            if n:
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
            else:
                out.write("[#6b7280]No tasks running.[/]")

        elif cmd == "/clear":
            out.clear()

        elif cmd == "/provider":
            self._handle_provider(arg, out)

        elif cmd == "/model":
            self._handle_model(arg, out)

        elif cmd == "/tools":
            self._show_tools()

        elif cmd == "/install":
            self._install_missing()

        elif cmd == "/selfmod":
            if not arg:
                out.write("[#6b7280]Usage: /selfmod <goal>[/]")
                out.write("[#6b7280]Example: /selfmod add a /history command that shows past tasks[/]")
                return
            self._run_selfmod(arg)

        elif cmd == "/undo":
            self._undo_selfmod()

        elif cmd == "/safe-mode":
            os.environ["CLIVE_EXPERIMENTAL_SELFMOD"] = "0"
            out.write("[#22c55e]✓[/] Self-modification disabled for this session.")

        elif cmd == "/evolve":
            if arg:
                out.write(f"[bold]Evolving {arg} driver...[/bold]")
                self._run_evolve(arg, out)
            else:
                out.write("[yellow]Usage: /evolve <driver> (shell, browser, all)[/yellow]")
            return

        elif cmd == "/dashboard":
            for line in render_lines():
                out.write(line)

        else:
            out.write(f"[#ef4444]Unknown command: {cmd}[/] — try /help")

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

    def _build_clive_context(self) -> str:
        """Build rich context about clive's actual configuration for triage."""
        lines = []

        lines.append("CURRENT CONFIGURATION:")
        lines.append(f"  Profile: {self._spec}")
        lines.append(f"  LLM provider: {llm.PROVIDER_NAME}, model: {llm.MODEL}")
        lines.append("")

        # Profiles
        lines.append("AVAILABLE PROFILES (switch with /profile <name>):")
        for name, cats in PROFILES.items():
            marker = " ← current" if name == self._spec else ""
            lines.append(f"  {name}: {', '.join(cats)}{marker}")
        lines.append("")

        # Categories
        lines.append("CATEGORIES (compose with /profile +<cat>):")
        lines.append(f"  {', '.join(sorted(CATEGORIES))}")
        lines.append("")

        # Current profile tools
        if self._resolved:
            pane_names = [p["name"] for p in self._resolved["panes"]]
            lines.append(f"CURRENT PANES (tmux windows): {', '.join(pane_names)}")
            lines.append("")

            if self._available_cmds:
                lines.append("AVAILABLE COMMANDS (installed):")
                for cmd in self._available_cmds:
                    lines.append(f"  {cmd['name']}: {cmd['description']}")
                lines.append("")

            if self._missing_cmds:
                lines.append("MISSING COMMANDS (not installed, use /install):")
                for cmd in self._missing_cmds:
                    lines.append(
                        f"  {cmd['name']}: {cmd['description']} — install: {cmd.get('install', '')}"
                    )
                lines.append("")

            if self._resolved["endpoints"]:
                lines.append("API ENDPOINTS (always available via curl):")
                for ep in self._resolved["endpoints"]:
                    lines.append(f"  {ep['name']}: {ep['description']} — {ep['usage']}")
                lines.append("")

        # Key features
        lines.append("HOW CLIVE WORKS:")
        lines.append("  - Runs tasks by driving CLI tools in tmux panes")
        lines.append("  - The LLM reads the terminal screen, reasons, and types commands")
        lines.append("  - Tasks are decomposed into subtasks that run in parallel across panes")
        lines.append("  - Configuration: .env file for LLM provider, -t flag or /profile for toolset")
        lines.append("  - Install tools: /install command or bash install.sh")
        lines.append("  - Email requires neomutt (add comms category: /profile +comms)")
        lines.append("  - Media/transcription requires yt-dlp, whisper, ffmpeg (add media category)")
        lines.append("  - LLM providers: " + ", ".join(LLM_PROVIDERS))
        lines.append("")

        lines.append("TUI SLASH COMMANDS: /help, /profile, /provider, /model, /tools, /install, /status, /cancel, /clear, /dashboard")

        return "\n".join(lines)

    def _show_tools(self) -> None:
        out = self.query_one("#output", RichLog)

        if not self._resolved:
            out.write("[#ef4444]No profile resolved.[/]")
            return

        # Panes
        pane_names = [p["name"] for p in self._resolved["panes"]]
        out.write(f"[#d97706]Panes:[/]  {', '.join(pane_names)}")
        out.write("")

        # Commands
        if self._available_cmds or self._missing_cmds:
            out.write("[#d97706]Commands:[/]")
            for cmd in self._available_cmds:
                out.write(
                    f"  [#22c55e]●[/] {cmd['name']:14s} [#6b7280]{cmd['description']}[/]"
                )
            for cmd in self._missing_cmds:
                install = cmd.get("install", "")
                out.write(
                    f"  [#ef4444]○[/] {cmd['name']:14s} [#3a3a4a]{install}[/]"
                )
            out.write("")

        # Endpoints
        if self._resolved["endpoints"]:
            out.write("[#d97706]APIs:[/]")
            for ep in self._resolved["endpoints"]:
                out.write(
                    f"  [#d97706]●[/] {ep['name']:14s} [#6b7280]{ep['description']}[/]"
                )
            out.write("")

        n_ok = len(self._available_cmds)
        n_miss = len(self._missing_cmds)
        if n_miss:
            out.write(
                f"[#6b7280]{n_ok} available, {n_miss} missing — run /install[/]"
            )
        else:
            out.write(f"[#6b7280]{n_ok} commands, all available[/]")

    def _install_missing(self) -> None:
        if not self._missing_cmds:
            self.query_one("#output", RichLog).write(
                "[#6b7280]Nothing to install.[/]"
            )
            return

        brew_pkgs = []
        pip_pkgs = []
        for cmd in self._missing_cmds:
            install = cmd.get("install", "")
            if install.startswith("brew install "):
                brew_pkgs.append(install.split("brew install ", 1)[1])
            elif install.startswith("pip install "):
                pip_pkgs.append(install.split("pip install ", 1)[1])

        if not brew_pkgs and not pip_pkgs:
            self.query_one("#output", RichLog).write(
                "[#6b7280]No auto-installable packages.[/]"
            )
            return

        self._do_install(brew_pkgs, pip_pkgs)

    @work(thread=True)
    def _do_install(self, brew_pkgs: list, pip_pkgs: list) -> None:
        out = self.query_one("#output", RichLog)

        if brew_pkgs:
            argv = ["brew", "install"] + brew_pkgs
            self.call_from_thread(
                out.write, f"[#d97706]$[/] {' '.join(argv)}"
            )
            self._run_subprocess(argv, out)

        if pip_pkgs:
            argv = ["pip3", "install"] + pip_pkgs
            self.call_from_thread(
                out.write, f"[#d97706]$[/] {' '.join(argv)}"
            )
            self._run_subprocess(argv, out)

        self.call_from_thread(out.write, "[#22c55e]✓ Install complete[/]")
        self.call_from_thread(self._resolve_profile)

    def _run_subprocess(self, argv: list[str], out: RichLog) -> None:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            self.call_from_thread(
                out.write, f"[#ef4444]✗ Command not found: {argv[0]}[/]"
            )
            return

        if proc.stdout:
            for line in proc.stdout:
                self.call_from_thread(out.write, line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            self.call_from_thread(
                out.write, f"[#ef4444]✗ Exit code: {proc.returncode}[/]"
            )

    # ── Self-modification ─────────────────────────────────────────────────

    def _run_selfmod(self, goal: str) -> None:
        from selfmod import is_enabled
        out = self.query_one("#output", RichLog)
        if not is_enabled():
            out.write(
                "[#f59e0b]⚠ Self-modification is disabled.[/]\n"
                "[#6b7280]Set CLIVE_EXPERIMENTAL_SELFMOD=1 in .env to enable.[/]"
            )
            return
        self._execute_selfmod(goal)

    @work(thread=True)
    def _execute_selfmod(self, goal: str) -> None:
        out = self.query_one("#output", RichLog)
        from selfmod.pipeline import run_pipeline

        def on_status(stage: str, msg: str) -> None:
            icon = {
                "analyzing": "◐",
                "proposing": "◐",
                "reviewing": "◑",
                "auditing": "◒",
                "gate": "◓",
                "applying": "●",
                "complete": "✓",
            }.get(stage, "·")
            color = "#22c55e" if stage == "complete" else "#d97706"
            self.call_from_thread(
                out.write, f"  [{color}]{icon}[/] [#6b7280]{stage}:[/] {msg}"
            )

        self.call_from_thread(out.write, "")
        self.call_from_thread(
            out.write, "[#d97706]Self-modification pipeline[/]"
        )

        result = run_pipeline(goal, on_status=on_status)

        self.call_from_thread(out.write, "")
        if result.success:
            self.call_from_thread(
                out.write,
                f"[#22c55e]✓ Applied:[/] {result.message}"
            )
            self.call_from_thread(
                out.write,
                f"[#6b7280]  Snapshot: {result.snapshot_tag} · "
                f"Tokens: {result.tokens['prompt'] + result.tokens['completion']:,}[/]"
            )
            self.call_from_thread(
                out.write,
                "[#6b7280]  Use /undo to roll back.[/]"
            )
        else:
            self.call_from_thread(
                out.write,
                f"[#ef4444]✗ {result.stage}:[/] {result.message}"
            )
        self.call_from_thread(out.write, "")

    def _undo_selfmod(self) -> None:
        out = self.query_one("#output", RichLog)
        try:
            from selfmod.workspace import rollback, list_snapshots
            snaps = list_snapshots()
            if not snaps:
                out.write("[#6b7280]No selfmod snapshots to undo.[/]")
                return
            tag = rollback()
            out.write(f"[#22c55e]✓[/] Rolled back to [#c9c9d6]{tag}[/]")
        except Exception as e:
            out.write(f"[#ef4444]✗ Undo failed: {e}[/]")

    def _run_evolve(self, driver: str, out: RichLog) -> None:
        """Run driver evolution in background thread."""
        import threading
        def _worker():
            try:
                from evolve import evolve_driver
                result = evolve_driver(driver, dry_run=False)
                if result["improved"]:
                    self.call_from_thread(out.write, f"[green]✓ {driver} driver improved: {result['baseline_score']:.3f} → {result['final_score']:.3f}[/green]")
                else:
                    self.call_from_thread(out.write, f"[yellow]No improvement found for {driver} (baseline: {result['baseline_score']:.3f})[/yellow]")
            except Exception as e:
                self.call_from_thread(out.write, f"[red]Evolution error: {e}[/red]")
        threading.Thread(target=_worker, daemon=True).start()
        out.write(f"[dim]Evolution running in background for {driver}...[/dim]")

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
            self._execute_task_inner(task, task_info, out)
        except Exception as e:
            self.call_from_thread(
                out.write, f"[#ef4444]✗ Unexpected error: {e}[/]"
            )
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            devnull.close()
            self.call_from_thread(self._finish_task, task_info)

    def _execute_task_inner(self, task: str, task_info: dict, out: RichLog) -> None:
        session_id = generate_session_id()
        session_dir = f"/tmp/clive/{session_id}"

        # Triage: classify the input before executing
        client = get_client()
        clive_context = self._build_clive_context()
        triage_msgs = [
            {"role": "system", "content": build_triage_prompt(clive_context)},
            {"role": "user", "content": task},
        ]
        try:
            triage_raw, pt, ct = chat(client, triage_msgs, max_tokens=512)
            task_info["pt"] += pt
            task_info["ct"] += ct
            # Parse JSON from response (strip markdown fences if present)
            clean = triage_raw.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            triage = json.loads(clean)
        except Exception:
            # If triage fails, fall through to execute
            triage = {"action": "execute", "task": task}

        action = triage.get("action", "execute")

        if action == "answer":
            self.call_from_thread(out.write, "")
            for line in triage.get("response", "").split("\n"):
                self.call_from_thread(out.write, line)
            self.call_from_thread(out.write, "")
            return

        if action == "clarify":
            question = triage.get("question", "Could you provide more details?")
            self.call_from_thread(
                out.write,
                f"\n[#d97706]?[/] {question}\n",
            )
            # Store pending state so next input continues this task
            self._pending = {"task": task}
            return

        # action == "execute" — may have a refined task description
        task = triage.get("task", task)
        task_info["desc"] = task  # update with refined version

        # Setup
        self.call_from_thread(
            out.write, "[#6b7280]Setting up session...[/]"
        )

        try:
            session, panes, _session_name = setup_session(self._resolved["panes"], session_dir=session_dir)
            tool_status = check_health(panes)
        except Exception as e:
            self.call_from_thread(
                out.write, f"[#ef4444]✗ Session failed: {e}[/]"
            )
            return

        tools_summary = build_tools_summary(
            tool_status, self._available_cmds, self._resolved["endpoints"]
        )

        if self._cancelled.is_set():
            return

        # Plan
        self.call_from_thread(out.write, "[#6b7280]Planning...[/]")

        try:
            plan = create_plan(
                task, panes, tool_status, tools_summary=tools_summary
            )
        except Exception as e:
            self.call_from_thread(
                out.write, f"[#ef4444]✗ Planning failed: {e}[/]"
            )
            return

        # Show plan
        self.call_from_thread(out.write, "")
        for s in plan.subtasks:
            deps = f" [#3a3a4a]→ {', '.join(s.depends_on)}[/]" if s.depends_on else ""
            self.call_from_thread(
                out.write,
                f"  [#3a3a4a]○[/] [#c9c9d6]{s.id}[/] [#6b7280]{s.pane}[/] {s.description[:55]}{deps}",
            )
        self.call_from_thread(out.write, "")

        if self._cancelled.is_set():
            return

        # Execute
        try:
            results = execute_plan(
                plan, panes, tool_status,
                on_event=lambda et, *a: self._on_event(et, task_info, *a),
                session_dir=session_dir
            )
        except Exception as e:
            self.call_from_thread(
                out.write, f"[#ef4444]✗ Execution failed: {e}[/]"
            )
            return

        if self._cancelled.is_set():
            return

        # Summarize
        self.call_from_thread(out.write, "")
        self.call_from_thread(
            out.write, "[#6b7280]Summarizing...[/]"
        )

        try:
            client = get_client()
            result_text = "\n\n".join(
                f"Subtask {r.subtask_id} [{r.status.value}]: {r.summary}"
                for r in results
            )
            messages = [
                {"role": "system", "content": build_summarizer_prompt()},
                {
                    "role": "user",
                    "content": f"Original task: {task}\n\nSubtask results:\n{result_text}",
                },
            ]
            summary, pt, ct = chat(client, messages)
            task_info["pt"] += pt
            task_info["ct"] += ct
        except Exception as e:
            summary = f"Summarization failed: {e}"

        completed = sum(
            1 for r in results if r.status == SubtaskStatus.COMPLETED
        )
        total = len(results)
        elapsed = time.time() - task_info["start"]
        total_tokens = task_info["pt"] + task_info["ct"]

        self.call_from_thread(out.write, "")
        self.call_from_thread(
            out.write,
            f"[#22c55e]✓ {completed}/{total} subtasks[/] [#3a3a4a]in {elapsed:.1f}s · {total_tokens:,} tokens[/]",
        )
        self.call_from_thread(out.write, "")
        for line in summary.split("\n"):
            self.call_from_thread(out.write, line)
        self.call_from_thread(out.write, "")

        # Cleanup session directory
        
        if session_dir and os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)


if __name__ == "__main__":
    app = CliveApp()
    app.run()
