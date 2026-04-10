"""TUI helper functions — context building and tools display.

Extracted from tui.py. These are pure text-building helpers that read
state from the CliveApp but don't mutate it, so they become free
functions taking the relevant state as parameters.
"""

import llm
from textual.widgets import RichLog

from llm import PROVIDERS as LLM_PROVIDERS
from toolsets import PROFILES, CATEGORIES


def build_clive_context(spec: str, resolved: dict | None,
                        available_cmds: list, missing_cmds: list) -> str:
    """Build rich context about clive's actual configuration for triage."""
    lines = []

    lines.append("CURRENT CONFIGURATION:")
    lines.append(f"  Profile: {spec}")
    lines.append(f"  LLM provider: {llm.PROVIDER_NAME}, model: {llm.MODEL}")
    lines.append("")

    # Profiles
    lines.append("AVAILABLE PROFILES (switch with /profile <name>):")
    for name, cats in PROFILES.items():
        marker = " ← current" if name == spec else ""
        lines.append(f"  {name}: {', '.join(cats)}{marker}")
    lines.append("")

    # Categories
    lines.append("CATEGORIES (compose with /profile +<cat>):")
    lines.append(f"  {', '.join(sorted(CATEGORIES))}")
    lines.append("")

    # Current profile tools
    if resolved:
        pane_names = [p["name"] for p in resolved["panes"]]
        lines.append(f"CURRENT PANES (tmux windows): {', '.join(pane_names)}")
        lines.append("")

        if available_cmds:
            lines.append("AVAILABLE COMMANDS (installed):")
            for cmd in available_cmds:
                lines.append(f"  {cmd['name']}: {cmd['description']}")
            lines.append("")

        if missing_cmds:
            lines.append("MISSING COMMANDS (not installed, use /install):")
            for cmd in missing_cmds:
                lines.append(
                    f"  {cmd['name']}: {cmd['description']} — install: {cmd.get('install', '')}"
                )
            lines.append("")

        if resolved["endpoints"]:
            lines.append("API ENDPOINTS (always available via curl):")
            for ep in resolved["endpoints"]:
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


def show_tools(out: RichLog, resolved: dict | None,
               available_cmds: list, missing_cmds: list) -> None:
    """Render the /tools command output to the RichLog."""
    if not resolved:
        out.write("[#ef4444]No profile resolved.[/]")
        return

    # Panes
    pane_names = [p["name"] for p in resolved["panes"]]
    out.write(f"[#d97706]Panes:[/]  {', '.join(pane_names)}")
    out.write("")

    # Commands
    if available_cmds or missing_cmds:
        out.write("[#d97706]Commands:[/]")
        for cmd in available_cmds:
            out.write(
                f"  [#22c55e]●[/] {cmd['name']:14s} [#6b7280]{cmd['description']}[/]"
            )
        for cmd in missing_cmds:
            install = cmd.get("install", "")
            out.write(
                f"  [#ef4444]○[/] {cmd['name']:14s} [#3a3a4a]{install}[/]"
            )
        out.write("")

    # Endpoints
    if resolved["endpoints"]:
        out.write("[#d97706]APIs:[/]")
        for ep in resolved["endpoints"]:
            out.write(
                f"  [#d97706]●[/] {ep['name']:14s} [#6b7280]{ep['description']}[/]"
            )
        out.write("")

    n_ok = len(available_cmds)
    n_miss = len(missing_cmds)
    if n_miss:
        out.write(
            f"[#6b7280]{n_ok} available, {n_miss} missing — run /install[/]"
        )
    else:
        out.write(f"[#6b7280]{n_ok} commands, all available[/]")
