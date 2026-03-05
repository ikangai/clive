#!/usr/bin/env python3
"""
clive TUI -- Terminal UI for the CLI Live Environment.

Setup screen: pick profile, see tool availability, install missing, enter task.
Run screen:   subtask DAG status, live worker log, summary on completion.

Usage:
    python tui.py
    python clive.py --tui   (if wired up)
"""

import subprocess
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TextArea,
)

LOGO = """\
 ██████╗██╗     ██╗██╗   ██╗███████╗
██╔════╝██║     ██║██║   ██║██╔════╝
██║     ██║     ██║██║   ██║█████╗
██║     ██║     ██║╚██╗ ██╔╝██╔══╝
╚██████╗███████╗██║ ╚████╔╝ ███████╗
 ╚═════╝╚══════╝╚═╝  ╚═══╝  ╚══════╝\
"""

from toolsets import (
    PROFILES,
    CATEGORIES,
    DEFAULT_TOOLSET,
    resolve_toolset,
    check_commands,
    build_tools_summary,
)

# ── CSS ──────────────────────────────────────────────────────────────────────

CSS = """
Screen {
    background: $surface;
}

#title-bar {
    dock: top;
    height: 1;
    background: $primary;
    color: $text;
    text-style: bold;
    padding: 0 1;
}

#logo {
    height: 7;
    content-align: center middle;
    color: $primary;
    text-style: bold;
    margin-bottom: 1;
}

/* ── Setup Screen ── */

#setup-container {
    padding: 1 2;
}

#profile-row {
    height: 3;
    margin-bottom: 1;
}

#profile-select {
    width: 24;
}

#category-input {
    width: 24;
    margin-left: 1;
}

#add-category-btn {
    margin-left: 1;
    min-width: 8;
}

#panes-label {
    margin-bottom: 1;
    color: $text-muted;
}

#tools-container {
    height: 1fr;
    margin-bottom: 1;
}

#commands-panel {
    width: 1fr;
    height: 100%;
    border: solid $primary;
    padding: 0 1;
}

#endpoints-panel {
    width: 30;
    height: 100%;
    border: solid $accent;
    padding: 0 1;
    margin-left: 1;
}

.section-title {
    text-style: bold;
    margin-bottom: 1;
}

#install-bar {
    height: 3;
    margin-bottom: 1;
}

#install-selected-btn {
    margin-right: 1;
}

#task-label {
    margin-bottom: 0;
    text-style: bold;
}

#task-input {
    height: 4;
    margin-bottom: 1;
}

#action-bar {
    height: 3;
    dock: bottom;
}

#run-btn {
    margin-right: 1;
}

#install-log {
    height: 8;
    border: solid $warning;
    display: none;
    margin-bottom: 1;
}

/* ── Run Screen ── */

#run-container {
    padding: 1 2;
}

#task-display {
    height: 2;
    color: $text-muted;
    margin-bottom: 1;
}

#plan-panel {
    height: auto;
    max-height: 12;
    border: solid $primary;
    padding: 0 1;
    margin-bottom: 1;
}

#log-panel {
    height: 1fr;
    border: solid $accent;
    padding: 0 1;
    margin-bottom: 1;
}

#status-bar {
    height: 1;
    dock: bottom;
    background: $primary;
    color: $text;
    padding: 0 1;
}

#cancel-btn {
    dock: bottom;
    margin-top: 1;
}

#summary-panel {
    height: 1fr;
    border: solid $success;
    padding: 1;
    display: none;
}

#back-btn {
    display: none;
    margin-top: 1;
}
"""

# ── Setup Screen ─────────────────────────────────────────────────────────────


class SetupScreen(Screen):
    """Profile selection, tool availability, install, task entry."""

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
    ]

    current_spec = reactive(DEFAULT_TOOLSET)

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-container"):
            yield Static(LOGO, id="logo")
            # Profile row
            with Horizontal(id="profile-row"):
                yield Select(
                    [(name, name) for name in PROFILES],
                    value=DEFAULT_TOOLSET,
                    id="profile-select",
                    prompt="Profile",
                )
                yield Input(
                    placeholder="+ category",
                    id="category-input",
                )
                yield Button("+Add", id="add-category-btn", variant="default")

            yield Label("Panes: ...", id="panes-label")

            # Tools
            with Horizontal(id="tools-container"):
                with Vertical(id="commands-panel"):
                    yield Label("Commands", classes="section-title")
                    yield VerticalScroll(id="commands-list")

                with Vertical(id="endpoints-panel"):
                    yield Label("APIs", classes="section-title")
                    yield Static("", id="endpoints-list")

            # Install bar
            with Horizontal(id="install-bar"):
                yield Button(
                    "Install All Missing",
                    id="install-selected-btn",
                    variant="warning",
                )

            yield RichLog(id="install-log", wrap=True, markup=True)

            # Task
            yield Label("Task:", id="task-label")
            yield TextArea(id="task-input")

            # Actions
            with Horizontal(id="action-bar"):
                yield Button("Run Task", id="run-btn", variant="success")
                yield Button("Quit", id="quit-btn", variant="error")

        yield Footer()

    def on_mount(self) -> None:
        self._refresh_tools()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "profile-select" and event.value != Select.BLANK:
            self.current_spec = str(event.value)
            self._refresh_tools()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-category-btn":
            self._add_category()
        elif event.button.id == "install-selected-btn":
            self._install_missing()
        elif event.button.id == "run-btn":
            self._run_task()
        elif event.button.id == "quit-btn":
            self.app.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "category-input":
            self._add_category()

    def _add_category(self) -> None:
        cat_input = self.query_one("#category-input", Input)
        cat = cat_input.value.strip()
        if not cat:
            return
        if cat in CATEGORIES or cat in PROFILES:
            new_spec = self.current_spec + "+" + cat
            try:
                resolve_toolset(new_spec)
                self.current_spec = new_spec
                cat_input.value = ""
                self._refresh_tools()
            except ValueError as e:
                self.notify(str(e), severity="error")
        else:
            self.notify(
                f"Unknown category: {cat!r}",
                severity="error",
            )

    def _refresh_tools(self) -> None:
        try:
            resolved = resolve_toolset(self.current_spec)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return

        # Store for later use
        self._resolved = resolved
        self._available_cmds, self._missing_cmds = check_commands(
            resolved["commands"]
        )

        # Panes
        pane_names = [p["name"] for p in resolved["panes"]]
        self.query_one("#panes-label", Label).update(
            f"Panes: {', '.join(pane_names)}"
        )

        # Commands
        container = self.query_one("#commands-list", VerticalScroll)
        container.remove_children()
        for cmd in self._available_cmds:
            container.mount(
                Static(
                    f"[green]+[/green] {cmd['name']:16s} {cmd['description']}"
                )
            )
        for cmd in self._missing_cmds:
            install = cmd.get("install", "")
            container.mount(
                Static(
                    f"[red]-[/red] {cmd['name']:16s} [dim]{install}[/dim]"
                )
            )

        if not self._available_cmds and not self._missing_cmds:
            container.mount(Static("[dim]No commands in this profile[/dim]"))

        # Enable/disable install button
        install_btn = self.query_one("#install-selected-btn", Button)
        install_btn.disabled = len(self._missing_cmds) == 0

        # Endpoints
        if resolved["endpoints"]:
            ep_text = "\n".join(
                f"[cyan]*[/cyan] {ep['name']}" for ep in resolved["endpoints"]
            )
        else:
            ep_text = "[dim]None[/dim]"
        self.query_one("#endpoints-list", Static).update(ep_text)

    def _install_missing(self) -> None:
        if not hasattr(self, "_missing_cmds") or not self._missing_cmds:
            return

        # Build install plan and show confirmation
        brew_pkgs = []
        pip_pkgs = []
        for cmd in self._missing_cmds:
            install = cmd.get("install", "")
            if install.startswith("brew install "):
                brew_pkgs.append(install.split("brew install ", 1)[1])
            elif install.startswith("pip install "):
                pip_pkgs.append(install.split("pip install ", 1)[1])

        if not brew_pkgs and not pip_pkgs:
            self.notify("No installable packages found", severity="warning")
            return

        summary_parts = []
        if brew_pkgs:
            summary_parts.append(f"brew install {' '.join(brew_pkgs)}")
        if pip_pkgs:
            summary_parts.append(f"pip3 install {' '.join(pip_pkgs)}")

        self.notify(
            "Installing: " + " && ".join(summary_parts),
            severity="information",
        )
        self._do_install(brew_pkgs, pip_pkgs)

    @work(thread=True)
    def _do_install(self, brew_pkgs: list, pip_pkgs: list) -> None:
        log = self.query_one("#install-log", RichLog)
        self.app.call_from_thread(self._show_install_log)

        if brew_pkgs:
            argv = ["brew", "install"] + brew_pkgs
            self.app.call_from_thread(
                log.write, f"[bold]$ {' '.join(argv)}[/bold]"
            )
            self._run_install(argv, log)

        if pip_pkgs:
            argv = ["pip3", "install"] + pip_pkgs
            self.app.call_from_thread(
                log.write, f"[bold]$ {' '.join(argv)}[/bold]"
            )
            self._run_install(argv, log)

        self.app.call_from_thread(log.write, "[green]Install complete.[/green]")
        self.app.call_from_thread(self._refresh_tools)

    def _run_install(self, argv: list[str], log: RichLog) -> None:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            self.app.call_from_thread(
                log.write, f"[red]Command not found: {argv[0]}[/red]"
            )
            return

        if proc.stdout:
            for line in proc.stdout:
                self.app.call_from_thread(log.write, line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            self.app.call_from_thread(
                log.write,
                f"[red]Exit code: {proc.returncode}[/red]",
            )

    def _show_install_log(self) -> None:
        log = self.query_one("#install-log", RichLog)
        log.clear()
        log.styles.display = "block"

    def _run_task(self) -> None:
        task_input = self.query_one("#task-input", TextArea)
        task = task_input.text.strip()
        if not task:
            self.notify("Enter a task first", severity="warning")
            return

        self.app.push_screen(
            RunScreen(
                task_text=task,
                toolset_spec=self.current_spec,
                resolved=self._resolved,
                available_cmds=self._available_cmds,
            )
        )


# ── Run Screen ───────────────────────────────────────────────────────────────


class RunScreen(Screen):
    """Live execution progress: plan DAG, worker log, summary."""

    BINDINGS = [
        Binding("ctrl+q", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        task_text: str,
        toolset_spec: str,
        resolved: dict,
        available_cmds: list,
    ):
        super().__init__()
        self.task_text = task_text
        self.toolset_spec = toolset_spec
        self.resolved = resolved
        self.available_cmds = available_cmds
        self._start_time = 0.0
        self._total_pt = 0
        self._total_ct = 0
        self._timer = None
        self._cancelled = threading.Event()

    def compose(self) -> ComposeResult:
        yield Static("CLIVE -- Running", id="title-bar")

        with Vertical(id="run-container"):
            yield Static(self.task_text[:120], id="task-display")

            with Vertical(id="plan-panel"):
                yield Label("Plan", classes="section-title")
                yield VerticalScroll(id="plan-list")

            yield RichLog(id="log-panel", wrap=True, markup=True)

            yield RichLog(id="summary-panel", wrap=True, markup=True)

            with Horizontal():
                yield Button("Cancel", id="cancel-btn", variant="error")
                yield Button(
                    "Back to Setup", id="back-btn", variant="primary"
                )

        yield Static("Elapsed: 0.0s  Tokens: 0", id="status-bar")

    def on_mount(self) -> None:
        self._start_time = time.time()
        self._timer = self.set_interval(1.0, self._update_status_bar)
        self._run_execution()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.action_cancel()
        elif event.button.id == "back-btn":
            self.app.pop_screen()

    def action_cancel(self) -> None:
        self._cancelled.set()
        # Kill the tmux session
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", "clive"],
                capture_output=True,
            )
        except Exception:
            pass
        self.app.pop_screen()

    def _update_status_bar(self) -> None:
        elapsed = time.time() - self._start_time
        total = self._total_pt + self._total_ct
        self.query_one("#status-bar", Static).update(
            f"Elapsed: {elapsed:.1f}s  "
            f"Tokens: {self._total_pt:,} prompt + {self._total_ct:,} completion = {total:,}"
        )

    def _on_event(self, event_type: str, *args) -> None:
        """Callback from executor thread — post updates to the TUI."""
        if self._cancelled.is_set():
            return
        self.app.call_from_thread(self._handle_event, event_type, *args)

    def _handle_event(self, event_type: str, *args) -> None:
        """Process executor events on the main thread."""
        plan_list = self.query_one("#plan-list", VerticalScroll)
        log = self.query_one("#log-panel", RichLog)

        if event_type == "subtask_start":
            sid, pane, desc = args
            widget_id = f"subtask-{sid}"
            try:
                plan_list.query_one(f"#{widget_id}", Static).update(
                    f"[yellow]~[/yellow] {sid} [{pane}] {desc[:60]}  [dim](run)[/dim]"
                )
            except Exception:
                pass

        elif event_type == "subtask_done":
            sid, summary, elapsed = args
            widget_id = f"subtask-{sid}"
            try:
                plan_list.query_one(f"#{widget_id}", Static).update(
                    f"[green]OK[/green] {sid} {summary[:60]}  [dim]{elapsed:.1f}s[/dim]"
                )
            except Exception:
                pass

        elif event_type == "subtask_fail":
            sid, error = args
            widget_id = f"subtask-{sid}"
            try:
                plan_list.query_one(f"#{widget_id}", Static).update(
                    f"[red]X[/red]  {sid} {error[:60]}"
                )
            except Exception:
                pass

        elif event_type == "subtask_skip":
            sid, reason = args
            widget_id = f"subtask-{sid}"
            try:
                plan_list.query_one(f"#{widget_id}", Static).update(
                    f"[dim]-- {sid} {reason[:60]}[/dim]"
                )
            except Exception:
                pass

        elif event_type == "turn":
            sid, turn_num, cmd_snippet = args
            log.write(f"[cyan][{sid}][/cyan] Turn {turn_num}: {cmd_snippet}")

        elif event_type == "tokens":
            sid, pt, ct = args
            self._total_pt += pt
            self._total_ct += ct

    @work(thread=True)
    def _run_execution(self) -> None:
        """Run the full clive pipeline in a background thread."""
        from session import setup_session, check_health, SESSION_NAME
        from planner import create_plan, display_plan
        from executor import execute_plan
        from models import SubtaskStatus
        from llm import get_client, chat
        from prompts import build_summarizer_prompt

        log = self.query_one("#log-panel", RichLog)

        # Phase 0: Setup session
        self.app.call_from_thread(log.write, "[bold]Setting up tmux session...[/bold]")

        try:
            session, panes = setup_session(self.resolved["panes"])
            tool_status = check_health(panes)
        except Exception as e:
            self.app.call_from_thread(
                log.write, f"[red]Session setup failed: {e}[/red]"
            )
            self.app.call_from_thread(self._show_back_button)
            return

        tools_summary = build_tools_summary(
            tool_status, self.available_cmds, self.resolved["endpoints"]
        )

        if self._cancelled.is_set():
            return

        # Phase 1: Planning
        self.app.call_from_thread(log.write, "[bold]Phase 1: Planning...[/bold]")

        try:
            plan = create_plan(
                self.task_text, panes, tool_status, tools_summary=tools_summary
            )
        except Exception as e:
            self.app.call_from_thread(
                log.write, f"[red]Planning failed: {e}[/red]"
            )
            self.app.call_from_thread(self._show_back_button)
            return

        # Populate plan panel
        self.app.call_from_thread(self._populate_plan, plan)

        if self._cancelled.is_set():
            return

        # Phase 2: Execution
        self.app.call_from_thread(log.write, "[bold]Phase 2: Executing...[/bold]")

        try:
            results = execute_plan(
                plan, panes, tool_status, on_event=self._on_event
            )
        except Exception as e:
            self.app.call_from_thread(
                log.write, f"[red]Execution failed: {e}[/red]"
            )
            self.app.call_from_thread(self._show_back_button)
            return

        if self._cancelled.is_set():
            return

        # Phase 3: Summarize
        self.app.call_from_thread(log.write, "[bold]Phase 3: Summarizing...[/bold]")

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
                    "content": f"Original task: {self.task_text}\n\nSubtask results:\n{result_text}",
                },
            ]
            summary, pt, ct = chat(client, messages)
            self._total_pt += pt
            self._total_ct += ct
        except Exception as e:
            summary = f"Summarization failed: {e}"

        # Show summary
        completed = sum(1 for r in results if r.status == SubtaskStatus.COMPLETED)
        total = len(results)

        self.app.call_from_thread(
            self._show_summary, summary, completed, total
        )

    def _populate_plan(self, plan) -> None:
        plan_list = self.query_one("#plan-list", VerticalScroll)
        for s in plan.subtasks:
            deps = f" (after {', '.join(s.depends_on)})" if s.depends_on else ""
            plan_list.mount(
                Static(
                    f"[ ] {s.id} [{s.pane}] {s.description[:55]}{deps}",
                    id=f"subtask-{s.id}",
                )
            )

    def _show_summary(self, summary: str, completed: int, total: int) -> None:
        # Stop the timer
        if self._timer:
            self._timer.stop()
            self._update_status_bar()

        summary_panel = self.query_one("#summary-panel", RichLog)
        summary_panel.styles.display = "block"
        summary_panel.write(
            f"[bold green]COMPLETE ({completed}/{total} subtasks)[/bold green]\n"
        )
        summary_panel.write(summary)

        self._show_back_button()

    def _show_back_button(self) -> None:
        self.query_one("#back-btn", Button).styles.display = "block"
        self.query_one("#cancel-btn", Button).styles.display = "none"


# ── App ──────────────────────────────────────────────────────────────────────


class CliveApp(App):
    """Clive TUI application."""

    TITLE = "clive"
    CSS = CSS

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def on_mount(self) -> None:
        self.push_screen(SetupScreen())


if __name__ == "__main__":
    app = CliveApp()
    app.run()
