"""TUI text constants — LOGO, help text, color scheme, CSS.

Extracted from tui.py to keep the App class file focused on behavior.
"""

from textual.design import ColorSystem


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
