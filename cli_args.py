"""Argument parser for clive — extracted from clive.py's __main__ block."""

import argparse
import os

from toolsets import CATEGORIES, DEFAULT_TOOLSET


def build_parser() -> argparse.ArgumentParser:
    """Construct the full clive CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="LLM agent that drives CLI tools via tmux",
        epilog=(
            "Examples:\n"
            "  clive \"list files in /tmp and show disk usage\"\n"
            "  clive -t standard \"browse example.com and summarize it\"\n"
            "  clive --dry-run \"check docker status\"   # preview plan only\n"
            "  clive --quiet --json \"count Python files\" # machine-readable\n"
            "  result=$(clive --quiet \"what is my IP\")   # capture result\n"
            "\n"
            "Compose toolsets with +: -t standard+media+ai\n"
            "Categories: " + ", ".join(sorted(CATEGORIES.keys()))
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", nargs="?", default=None, help="Task for the agent to perform")
    parser.add_argument(
        "-t", "--toolset", default=DEFAULT_TOOLSET, metavar="SPEC",
        help=f"Toolset spec: profile name, category combo with +, or mix (default: {DEFAULT_TOOLSET})",
    )
    parser.add_argument("--list-toolsets", action="store_true", help="List available profiles and exit")
    parser.add_argument("--list-tools", action="store_true", help="List all tools across all three surfaces and exit")
    parser.add_argument("--tui", action="store_true", help="Launch the interactive TUI instead of CLI mode")
    parser.add_argument("--selfmod", metavar="GOAL", help="Self-modify clive (experimental, requires CLIVE_EXPERIMENTAL_SELFMOD=1)")
    parser.add_argument("--undo", action="store_true", help="Roll back last self-modification")
    parser.add_argument("--safe-mode", action="store_true", help="Disable self-modification for this run")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet mode: telemetry to stderr, only result to stdout")
    parser.add_argument("--oneline", action="store_true", help="Single-line result output")
    parser.add_argument("--bool", action="store_true", help="Yes/No output, exit 0=yes 1=no")
    parser.add_argument("--json", action="store_true", help="Structured JSON result output")
    parser.add_argument("--conversational", action="store_true", help="Conversational mode for clive-to-clive peer dialogue (auto-detected via isatty)")
    parser.add_argument("--list-skills", action="store_true", help="List available skills")
    parser.add_argument("--evolve", metavar="DRIVER", help="Evolve a driver prompt (shell, browser, all)")
    parser.add_argument("--remote", metavar="HOST", help="Run task on remote clive via SSH (user@host)")
    parser.add_argument("--schedule", metavar="CRON", help="Schedule task with cron expression")
    parser.add_argument("--list-schedules", action="store_true", help="List scheduled tasks")
    parser.add_argument("--remove-schedule", metavar="NAME", help="Remove a scheduled task")
    parser.add_argument("--pause-schedule", metavar="NAME", help="Pause a scheduled task")
    parser.add_argument("--resume-schedule", metavar="NAME", help="Resume a paused task")
    parser.add_argument("--run-now", metavar="NAME", help="Run a scheduled task immediately")
    parser.add_argument("--history", metavar="NAME", help="Show run history")
    parser.add_argument("--notify", metavar="METHOD", default="", help="Notification: email:addr or file:/path")
    parser.add_argument("--name", metavar="NAME", help="Name this instance (makes it addressable and conversational)")
    parser.add_argument("--stop", metavar="NAME", help="Stop a named instance by sending SIGTERM")
    parser.add_argument("--setup", metavar="TOOL", help="Configure a tool (e.g. --setup email)")
    parser.add_argument("--dashboard", action="store_true", help="Show running instances and exit")
    parser.add_argument("--serve", action="store_true", help="Start server mode with worker pool")
    parser.add_argument("--instances", action="store_true", help="List running clive instances and exit")
    parser.add_argument("--status", action="store_true", help="Show server health status and exit")
    parser.add_argument("--workers", type=int, default=4, metavar="N", help="Number of workers in server mode (default: 4)")
    parser.add_argument("--queue-dir", default=os.path.expanduser("~/.clive/queue"), metavar="DIR", help="Job queue directory (default: ~/.clive/queue)")
    parser.add_argument("--dry-run", action="store_true", help="Show the execution plan without running it")
    parser.add_argument("--version", action="version", version="clive 0.2.0")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to stderr")
    parser.add_argument("--max-tokens", type=int, default=50000, help="Maximum total tokens before aborting (default: 50000)")
    parser.add_argument("--list-sessions", action="store_true", help="List persistent chat sessions (most recent first) and exit")
    parser.add_argument("--new-session", action="store_true", help="Create a new persistent chat session and print its id")
    parser.add_argument("--resume-session", metavar="SID", help="Resume a persistent chat session by id (binds subsequent tasks to it)")
    return parser
