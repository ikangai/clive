"""TUI text constants — LOGO, color scheme, CSS.

Slash-command help was previously a hand-maintained HELP_TEXT block here.
It now lives in commands.render_help() which generates the same rendering
from the registered SlashCommand entries, so the dispatch list and help
list cannot drift apart.
"""

from textual.design import ColorSystem


LOGO = """\
[#e8915a] ██████╗██╗     ██╗██╗   ██╗███████╗[/]
[#d97706]██╔════╝██║     ██║██║   ██║██╔════╝[/]
[#d97706]██║     ██║     ██║██║   ██║█████╗  [/]
[#c2650a]██║     ██║     ██║╚██╗ ██╔╝██╔══╝  [/]
[#c2650a]╚██████╗███████╗██║ ╚████╔╝ ███████╗[/]
[#b45309] ╚═════╝╚══════╝╚═╝  ╚═══╝  ╚══════╝[/]"""

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
