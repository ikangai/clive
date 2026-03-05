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

import subprocess
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.design import ColorSystem
from textual.screen import Screen
from textual.widgets import (
    Footer,
    Input,
    RichLog,
    Static,
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
  [#c9c9d6]/profile[/] [#6b7280]<name>[/]      Switch toolset profile
  [#c9c9d6]/profile[/] [#6b7280]+<cat>[/]      Add category to current profile
  [#c9c9d6]/tools[/]                Show available and missing tools
  [#c9c9d6]/install[/]              Install missing CLI tools
  [#c9c9d6]/help[/]                 Show this help

[#d97706]Profiles:[/]  {profiles}
[#d97706]Categories:[/] {categories}

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

# ── Main Screen ──────────────────────────────────────────────────────────────


class MainScreen(Screen):

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
        self._running = False
        self._cancelled = threading.Event()
        self._start_time = 0.0
        self._total_pt = 0
        self._total_ct = 0
        self._timer = None

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
        elif self._running:
            out.write("[#ef4444]A task is already running.[/]")
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

        elif cmd == "/tools":
            self._show_tools()

        elif cmd == "/install":
            self._install_missing()

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
            self.app.call_from_thread(
                out.write, f"[#d97706]$[/] {' '.join(argv)}"
            )
            self._run_subprocess(argv, out)

        if pip_pkgs:
            argv = ["pip3", "install"] + pip_pkgs
            self.app.call_from_thread(
                out.write, f"[#d97706]$[/] {' '.join(argv)}"
            )
            self._run_subprocess(argv, out)

        self.app.call_from_thread(out.write, "[#22c55e]✓ Install complete[/]")
        self.app.call_from_thread(self._resolve_profile)

    def _run_subprocess(self, argv: list[str], out: RichLog) -> None:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            self.app.call_from_thread(
                out.write, f"[#ef4444]✗ Command not found: {argv[0]}[/]"
            )
            return

        if proc.stdout:
            for line in proc.stdout:
                self.app.call_from_thread(out.write, line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            self.app.call_from_thread(
                out.write, f"[#ef4444]✗ Exit code: {proc.returncode}[/]"
            )

    # ── Task execution ───────────────────────────────────────────────────

    def _run_task(self, task: str) -> None:
        self._running = True
        self._cancelled.clear()
        self._total_pt = 0
        self._total_ct = 0
        self._start_time = time.time()
        self._timer = self.set_interval(1.0, self._update_status)
        self._execute_task(task)

    def _finish_task(self) -> None:
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer = None
        self._update_status()

    def _update_status(self) -> None:
        parts = [f"[#6b7280]profile[/] {self._spec}"]
        if self._running:
            elapsed = time.time() - self._start_time
            total = self._total_pt + self._total_ct
            parts.append(f"[#d97706]running[/] {elapsed:.0f}s")
            if total:
                parts.append(f"[#6b7280]tokens[/] {total:,}")
        self.query_one("#status-bar", Static).update("  ".join(parts))

    def _on_event(self, event_type: str, *args) -> None:
        if self._cancelled.is_set():
            return
        self.app.call_from_thread(self._handle_event, event_type, *args)

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
            _, pt, ct = args
            self._total_pt += pt
            self._total_ct += ct

    @work(thread=True)
    def _execute_task(self, task: str) -> None:
        from session import setup_session, check_health
        from planner import create_plan
        from executor import execute_plan
        from models import SubtaskStatus
        from llm import get_client, chat
        from prompts import build_summarizer_prompt

        out = self.query_one("#output", RichLog)

        # Setup
        self.app.call_from_thread(
            out.write, "[#6b7280]Setting up session...[/]"
        )

        try:
            session, panes = setup_session(self._resolved["panes"])
            tool_status = check_health(panes)
        except Exception as e:
            self.app.call_from_thread(
                out.write, f"[#ef4444]✗ Session failed: {e}[/]"
            )
            self.app.call_from_thread(self._finish_task)
            return

        tools_summary = build_tools_summary(
            tool_status, self._available_cmds, self._resolved["endpoints"]
        )

        if self._cancelled.is_set():
            self.app.call_from_thread(self._finish_task)
            return

        # Plan
        self.app.call_from_thread(out.write, "[#6b7280]Planning...[/]")

        try:
            plan = create_plan(
                task, panes, tool_status, tools_summary=tools_summary
            )
        except Exception as e:
            self.app.call_from_thread(
                out.write, f"[#ef4444]✗ Planning failed: {e}[/]"
            )
            self.app.call_from_thread(self._finish_task)
            return

        # Show plan
        self.app.call_from_thread(out.write, "")
        for s in plan.subtasks:
            deps = f" [#3a3a4a]→ {', '.join(s.depends_on)}[/]" if s.depends_on else ""
            self.app.call_from_thread(
                out.write,
                f"  [#3a3a4a]○[/] [#c9c9d6]{s.id}[/] [#6b7280]{s.pane}[/] {s.description[:55]}{deps}",
            )
        self.app.call_from_thread(out.write, "")

        if self._cancelled.is_set():
            self.app.call_from_thread(self._finish_task)
            return

        # Execute
        try:
            results = execute_plan(
                plan, panes, tool_status, on_event=self._on_event
            )
        except Exception as e:
            self.app.call_from_thread(
                out.write, f"[#ef4444]✗ Execution failed: {e}[/]"
            )
            self.app.call_from_thread(self._finish_task)
            return

        if self._cancelled.is_set():
            self.app.call_from_thread(self._finish_task)
            return

        # Summarize
        self.app.call_from_thread(out.write, "")
        self.app.call_from_thread(
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
            self._total_pt += pt
            self._total_ct += ct
        except Exception as e:
            summary = f"Summarization failed: {e}"

        completed = sum(
            1 for r in results if r.status == SubtaskStatus.COMPLETED
        )
        total = len(results)
        elapsed = time.time() - self._start_time

        self.app.call_from_thread(out.write, "")
        self.app.call_from_thread(
            out.write,
            f"[#22c55e]✓ {completed}/{total} subtasks[/] [#3a3a4a]in {elapsed:.1f}s · {self._total_pt + self._total_ct:,} tokens[/]",
        )
        self.app.call_from_thread(out.write, "")
        for line in summary.split("\n"):
            self.app.call_from_thread(out.write, line)
        self.app.call_from_thread(out.write, "")
        self.app.call_from_thread(self._finish_task)


# ── App ──────────────────────────────────────────────────────────────────────


class CliveApp(App):
    """Clive TUI application."""

    TITLE = "clive"
    CSS = CSS

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **CLIVE_THEME.generate()}

    def on_mount(self) -> None:
        self.push_screen(MainScreen())


if __name__ == "__main__":
    app = CliveApp()
    app.run()
