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
import os
import subprocess
import sys
import threading
import time

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

from llm import PROVIDERS as LLM_PROVIDERS
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
                        ["tmux", "kill-session", "-t", "clive"],
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
        import llm

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
        import llm

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
        import llm
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
        from session import setup_session, check_health
        from planner import create_plan
        from executor import execute_plan
        from models import SubtaskStatus
        from llm import get_client, chat
        from prompts import build_summarizer_prompt

        # Setup
        self.call_from_thread(
            out.write, "[#6b7280]Setting up session...[/]"
        )

        try:
            session, panes = setup_session(self._resolved["panes"])
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
                on_event=lambda et, *a: self._on_event(et, task_info, *a)
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


if __name__ == "__main__":
    app = CliveApp()
    app.run()
