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
from textual.design import ColorSystem
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    RichLog,
    Rule,
    Select,
    Static,
    TextArea,
)

LOGO = """\
[b #e8915a] ██████╗██╗     ██╗██╗   ██╗███████╗[/]
[b #e8915a]██╔════╝██║     ██║██║   ██║██╔════╝[/]
[b #d97706]██║     ██║     ██║██║   ██║█████╗  [/]
[b #d97706]██║     ██║     ██║╚██╗ ██╔╝██╔══╝  [/]
[b #c2650a]╚██████╗███████╗██║ ╚████╔╝ ███████╗[/]
[b #c2650a] ╚═════╝╚══════╝╚═╝  ╚═══╝  ╚══════╝[/]\
"""

from toolsets import (
    PROFILES,
    CATEGORIES,
    DEFAULT_TOOLSET,
    resolve_toolset,
    check_commands,
    build_tools_summary,
)

# ── Theme ───────────────────────────────────────────────────────────────────

CLIVE_DARK = ColorSystem(
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

/* ── Setup Screen ── */

#setup-container {
    padding: 1 3;
    background: #111118;
}

#logo {
    height: 7;
    content-align: center middle;
    margin-bottom: 1;
}

#profile-row {
    height: 3;
    margin-bottom: 1;
}

#profile-select {
    width: 28;
    background: #1c1c27;
    border: tall #2a2a3a;
    color: #c9c9d6;
}

#profile-select:focus {
    border: tall #d97706;
}

#category-input {
    width: 28;
    margin-left: 2;
    background: #1c1c27;
    border: tall #2a2a3a;
    color: #c9c9d6;
}

#category-input:focus {
    border: tall #d97706;
}

#add-category-btn {
    margin-left: 1;
    min-width: 6;
    background: #2a2a3a;
    color: #c9c9d6;
    border: none;
}

#add-category-btn:hover {
    background: #d97706;
    color: #111118;
}

#spec-label {
    color: #6b7280;
    margin-bottom: 1;
    height: 1;
}

Rule {
    color: #2a2a3a;
    margin: 0 0 1 0;
}

.section-header {
    color: #d97706;
    text-style: bold;
    margin-bottom: 1;
    text-opacity: 85%;
}

#tools-container {
    height: 1fr;
    margin-bottom: 1;
}

#commands-panel {
    width: 1fr;
    height: 100%;
    background: #16161e;
    border: round #2a2a3a;
    padding: 1 2;
}

#endpoints-panel {
    width: 34;
    height: 100%;
    background: #16161e;
    border: round #2a2a3a;
    padding: 1 2;
    margin-left: 1;
}

#commands-list {
    scrollbar-size: 1 1;
}

#install-bar {
    height: auto;
    margin-bottom: 1;
}

#install-selected-btn {
    background: #2a2a3a;
    color: #f59e0b;
    border: none;
    min-width: 22;
}

#install-selected-btn:hover {
    background: #f59e0b;
    color: #111118;
}

#install-selected-btn.-disabled {
    background: #1c1c27;
    color: #3a3a4a;
}

#install-log {
    height: 8;
    background: #16161e;
    border: round #2a2a3a;
    display: none;
    margin-bottom: 1;
    padding: 0 1;
}

#task-section-label {
    color: #6b7280;
    text-style: bold;
    margin-bottom: 0;
    height: 1;
}

#task-input {
    height: 4;
    margin-bottom: 1;
    background: #1c1c27;
    border: tall #2a2a3a;
    color: #c9c9d6;
}

#task-input:focus {
    border: tall #d97706;
}

#action-bar {
    height: 3;
    dock: bottom;
}

#run-btn {
    margin-right: 1;
    background: #d97706;
    color: #111118;
    text-style: bold;
    border: none;
    min-width: 16;
}

#run-btn:hover {
    background: #e8915a;
}

#quit-btn {
    background: #2a2a3a;
    color: #6b7280;
    border: none;
    min-width: 10;
}

#quit-btn:hover {
    background: #ef4444;
    color: #ffffff;
}

Footer {
    background: #16161e;
    color: #6b7280;
}

Footer > .footer--key {
    background: #2a2a3a;
    color: #d97706;
}

/* ── Run Screen ── */

#run-container {
    padding: 1 3;
    background: #111118;
}

#run-header {
    height: 1;
    color: #d97706;
    text-style: bold;
    margin-bottom: 1;
}

#task-display {
    height: 2;
    color: #6b7280;
    margin-bottom: 1;
}

#phase-label {
    height: 1;
    color: #d97706;
    text-style: bold;
    margin-bottom: 1;
}

#plan-panel {
    height: auto;
    max-height: 14;
    background: #16161e;
    border: round #2a2a3a;
    padding: 1 2;
    margin-bottom: 1;
}

#log-panel {
    height: 1fr;
    background: #16161e;
    border: round #2a2a3a;
    padding: 0 2;
    margin-bottom: 1;
}

#status-bar {
    height: 1;
    dock: bottom;
    background: #16161e;
    color: #6b7280;
    padding: 0 3;
}

#run-action-bar {
    height: 3;
    margin-bottom: 0;
}

#cancel-btn {
    background: #2a2a3a;
    color: #ef4444;
    border: none;
    min-width: 12;
}

#cancel-btn:hover {
    background: #ef4444;
    color: #ffffff;
}

#summary-panel {
    height: 1fr;
    background: #16161e;
    border: round #22c55e 50%;
    padding: 1 2;
    display: none;
}

#back-btn {
    display: none;
    background: #d97706;
    color: #111118;
    text-style: bold;
    border: none;
    min-width: 16;
}

#back-btn:hover {
    background: #e8915a;
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
            yield Static(LOGO, id="logo", markup=True)

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
                yield Button("+", id="add-category-btn", variant="default")

            yield Label("", id="spec-label")
            yield Rule()

            # Tools
            with Horizontal(id="tools-container"):
                with Vertical(id="commands-panel"):
                    yield Label("COMMANDS", classes="section-header")
                    yield VerticalScroll(id="commands-list")

                with Vertical(id="endpoints-panel"):
                    yield Label("APIS", classes="section-header")
                    yield Static("", id="endpoints-list")

            # Install bar
            with Horizontal(id="install-bar"):
                yield Button(
                    "Install Missing",
                    id="install-selected-btn",
                    variant="warning",
                )

            yield RichLog(id="install-log", wrap=True, markup=True)

            # Task
            yield Label("TASK", id="task-section-label")
            yield TextArea(id="task-input")

            # Actions
            with Horizontal(id="action-bar"):
                yield Button("Run", id="run-btn", variant="success")
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

        # Spec + panes label
        pane_names = [p["name"] for p in resolved["panes"]]
        self.query_one("#spec-label", Label).update(
            f"  {self.current_spec}  ·  panes: {', '.join(pane_names)}"
        )

        # Commands
        container = self.query_one("#commands-list", VerticalScroll)
        container.remove_children()
        for cmd in self._available_cmds:
            container.mount(
                Static(
                    f"[#22c55e]●[/] [#c9c9d6]{cmd['name']:14s}[/] [#6b7280]{cmd['description']}[/]"
                )
            )
        for cmd in self._missing_cmds:
            install = cmd.get("install", "")
            container.mount(
                Static(
                    f"[#ef4444]○[/] [#6b7280]{cmd['name']:14s}[/] [#3a3a4a]{install}[/]"
                )
            )

        if not self._available_cmds and not self._missing_cmds:
            container.mount(Static("[#3a3a4a]No commands in this profile[/]"))

        # Enable/disable install button
        install_btn = self.query_one("#install-selected-btn", Button)
        install_btn.disabled = len(self._missing_cmds) == 0

        # Endpoints
        if resolved["endpoints"]:
            ep_text = "\n".join(
                f"[#d97706]●[/] [#c9c9d6]{ep['name']}[/]"
                for ep in resolved["endpoints"]
            )
        else:
            ep_text = "[#3a3a4a]None[/]"
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
                log.write, f"[#d97706]$ {' '.join(argv)}[/]"
            )
            self._run_install(argv, log)

        if pip_pkgs:
            argv = ["pip3", "install"] + pip_pkgs
            self.app.call_from_thread(
                log.write, f"[#d97706]$ {' '.join(argv)}[/]"
            )
            self._run_install(argv, log)

        self.app.call_from_thread(log.write, "[#22c55e]✓ Install complete[/]")
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
                log.write, f"[#ef4444]✗ Command not found: {argv[0]}[/]"
            )
            return

        if proc.stdout:
            for line in proc.stdout:
                self.app.call_from_thread(log.write, line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            self.app.call_from_thread(
                log.write,
                f"[#ef4444]✗ Exit code: {proc.returncode}[/]",
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
        with Vertical(id="run-container"):
            yield Static("[b #d97706]❯ clive[/]", id="run-header")
            yield Static(f"[#6b7280]{self.task_text[:120]}[/]", id="task-display")
            yield Static("[#d97706]PLAN[/]", id="phase-label")

            with Vertical(id="plan-panel"):
                yield VerticalScroll(id="plan-list")

            yield RichLog(id="log-panel", wrap=True, markup=True)

            yield RichLog(id="summary-panel", wrap=True, markup=True)

            with Horizontal(id="run-action-bar"):
                yield Button("Cancel", id="cancel-btn", variant="error")
                yield Button(
                    "← Back", id="back-btn", variant="primary"
                )

        yield Static(
            "[#6b7280]elapsed[/] 0.0s  [#6b7280]tokens[/] 0",
            id="status-bar",
        )

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
            f"[#6b7280]elapsed[/] {elapsed:.1f}s  "
            f"[#6b7280]tokens[/] [#c9c9d6]{self._total_pt:,}[/]"
            f"[#6b7280]+[/][#c9c9d6]{self._total_ct:,}[/]"
            f"[#6b7280]=[/][#d97706]{total:,}[/]"
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
                    f"[#f59e0b]◐[/] [#c9c9d6]{sid}[/] [#6b7280]{pane}[/] {desc[:60]}"
                )
            except Exception:
                pass

        elif event_type == "subtask_done":
            sid, summary, elapsed = args
            widget_id = f"subtask-{sid}"
            try:
                plan_list.query_one(f"#{widget_id}", Static).update(
                    f"[#22c55e]✓[/] [#c9c9d6]{sid}[/] {summary[:55]} [#3a3a4a]{elapsed:.1f}s[/]"
                )
            except Exception:
                pass

        elif event_type == "subtask_fail":
            sid, error = args
            widget_id = f"subtask-{sid}"
            try:
                plan_list.query_one(f"#{widget_id}", Static).update(
                    f"[#ef4444]✗[/] [#c9c9d6]{sid}[/] [#ef4444]{error[:60]}[/]"
                )
            except Exception:
                pass

        elif event_type == "subtask_skip":
            sid, reason = args
            widget_id = f"subtask-{sid}"
            try:
                plan_list.query_one(f"#{widget_id}", Static).update(
                    f"[#3a3a4a]– {sid} {reason[:60]}[/]"
                )
            except Exception:
                pass

        elif event_type == "turn":
            sid, turn_num, cmd_snippet = args
            log.write(
                f"[#d97706]❯[/] [#6b7280]{sid}[/] [#3a3a4a]t{turn_num}[/] {cmd_snippet}"
            )

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
        self.app.call_from_thread(
            log.write, "[#6b7280]Setting up tmux session...[/]"
        )

        try:
            session, panes = setup_session(self.resolved["panes"])
            tool_status = check_health(panes)
        except Exception as e:
            self.app.call_from_thread(
                log.write, f"[#ef4444]✗ Session setup failed: {e}[/]"
            )
            self.app.call_from_thread(self._show_back_button)
            return

        tools_summary = build_tools_summary(
            tool_status, self.available_cmds, self.resolved["endpoints"]
        )

        if self._cancelled.is_set():
            return

        # Phase 1: Planning
        self.app.call_from_thread(self._set_phase, "PLANNING")
        self.app.call_from_thread(
            log.write, "[#6b7280]Decomposing task into subtasks...[/]"
        )

        try:
            plan = create_plan(
                self.task_text, panes, tool_status, tools_summary=tools_summary
            )
        except Exception as e:
            self.app.call_from_thread(
                log.write, f"[#ef4444]✗ Planning failed: {e}[/]"
            )
            self.app.call_from_thread(self._show_back_button)
            return

        # Populate plan panel
        self.app.call_from_thread(self._populate_plan, plan)

        if self._cancelled.is_set():
            return

        # Phase 2: Execution
        self.app.call_from_thread(self._set_phase, "EXECUTING")

        try:
            results = execute_plan(
                plan, panes, tool_status, on_event=self._on_event
            )
        except Exception as e:
            self.app.call_from_thread(
                log.write, f"[#ef4444]✗ Execution failed: {e}[/]"
            )
            self.app.call_from_thread(self._show_back_button)
            return

        if self._cancelled.is_set():
            return

        # Phase 3: Summarize
        self.app.call_from_thread(self._set_phase, "SUMMARIZING")

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
        completed = sum(
            1 for r in results if r.status == SubtaskStatus.COMPLETED
        )
        total = len(results)

        self.app.call_from_thread(
            self._show_summary, summary, completed, total
        )

    def _set_phase(self, phase: str) -> None:
        self.query_one("#phase-label", Static).update(
            f"[#d97706]{phase}[/]"
        )

    def _populate_plan(self, plan) -> None:
        plan_list = self.query_one("#plan-list", VerticalScroll)
        for s in plan.subtasks:
            deps = (
                f" [#3a3a4a]→ {', '.join(s.depends_on)}[/]"
                if s.depends_on
                else ""
            )
            plan_list.mount(
                Static(
                    f"[#3a3a4a]○[/] [#c9c9d6]{s.id}[/] [#6b7280]{s.pane}[/] {s.description[:55]}{deps}",
                    id=f"subtask-{s.id}",
                )
            )

    def _show_summary(self, summary: str, completed: int, total: int) -> None:
        # Stop the timer
        if self._timer:
            self._timer.stop()
            self._update_status_bar()

        self._set_phase("COMPLETE")

        summary_panel = self.query_one("#summary-panel", RichLog)
        summary_panel.styles.display = "block"
        summary_panel.write(
            f"[b #22c55e]✓ {completed}/{total} subtasks completed[/]\n"
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

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **CLIVE_DARK.generate()}

    def on_mount(self) -> None:
        self.push_screen(SetupScreen())


if __name__ == "__main__":
    app = CliveApp()
    app.run()
