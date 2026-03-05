#!/usr/bin/env python3
"""
clive -- CLI Live Environment.

tmux agent loop with planning, subtask decomposition, and parallel execution.
The pane is the universal agent interface: the LLM reads the terminal screen,
reasons about what it sees, and types commands. No structured APIs needed.

Usage:
    python clive.py "your task description"
    python clive.py -t standard "browse example.com and summarize it"
    python clive.py -t web+comms "check email and research a topic"
    python clive.py -t standard+media+ai "transcribe video and summarize"
    python clive.py --list-toolsets
    python clive.py --list-tools
    python clive.py                          # uses built-in example task

Toolsets (compose with +):
    Profiles:    minimal, standard, full, research, business, creative, remote
    Categories:  core, web, data, docs, comms, media, productivity,
                 finance, social, translation, search, images, dev,
                 voice, ai, sync, info, remote

    Watch in real-time:
        tmux attach -t clive

Requirements:
    pip install -r requirements.txt

Environment:
    LLM_PROVIDER, AGENT_MODEL, and provider API keys (set in .env file)
"""

import argparse
import time

from dotenv import load_dotenv

load_dotenv()

from session import setup_session, check_health, SESSION_NAME
from toolsets import (
    resolve_toolset, check_commands, build_tools_summary,
    print_availability, list_toolsets, list_categories,
    DEFAULT_TOOLSET, PROFILES, CATEGORIES,
)
from planner import create_plan, display_plan
from executor import execute_plan
from models import SubtaskStatus
from llm import get_client, chat
from prompts import build_summarizer_prompt


def run(task: str, toolset_spec: str = DEFAULT_TOOLSET):
    # Resolve toolset spec into three surfaces
    resolved = resolve_toolset(toolset_spec)

    print(f"\n{'=' * 60}")
    print(f"Setting up session: {SESSION_NAME}")
    print(f"{'~' * 60}")

    # Create tmux session with pane tools only
    session, panes = setup_session(resolved["panes"])

    print(f"\nHealth check:")
    tool_status = check_health(panes)

    # Auto-detect which commands are installed
    available_cmds, missing_cmds = check_commands(resolved["commands"])

    print()
    print_availability(
        tool_status, available_cmds, missing_cmds,
        resolved["endpoints"], resolved["categories"],
    )

    # Build enriched tools summary for the LLM (all three surfaces)
    tools_summary = build_tools_summary(
        tool_status, available_cmds, resolved["endpoints"],
    )

    print(f"{'~' * 60}")
    print(f"Task: {task}")
    print(f"Watch: tmux attach -t {SESSION_NAME}")
    print(f"{'=' * 60}\n")

    start_time = time.time()

    # Phase 1: Planning
    print("Phase 1: Planning...")
    plan = create_plan(task, panes, tool_status, tools_summary=tools_summary)
    display_plan(plan)

    # Phase 2: Execution
    print("Phase 2: Executing...")
    results = execute_plan(plan, panes, tool_status)

    # Phase 3: Summarization
    print("\nPhase 3: Summarizing...")
    summary = _summarize(task, results)

    elapsed = time.time() - start_time
    total_pt = sum(r.prompt_tokens for r in results)
    total_ct = sum(r.completion_tokens for r in results)
    completed = sum(1 for r in results if r.status == SubtaskStatus.COMPLETED)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"TASK COMPLETE ({completed}/{total} subtasks succeeded)")
    print(f"{'=' * 60}")
    print(summary)
    print(f"{'~' * 60}")
    print(f"Time:   {elapsed:.1f}s")
    print(f"Tokens: {total_pt} prompt + {total_ct} completion = {total_pt + total_ct} total")
    print(f"{'=' * 60}\n")

    return summary


def _summarize(task: str, results: list) -> str:
    """Final LLM call to synthesize all subtask results."""
    client = get_client()

    result_text = "\n\n".join(
        f"Subtask {r.subtask_id} [{r.status.value}]: {r.summary}"
        for r in results
    )

    messages = [
        {"role": "system", "content": build_summarizer_prompt()},
        {"role": "user", "content": f"Original task: {task}\n\nSubtask results:\n{result_text}"},
    ]

    content, _, _ = chat(client, messages)
    return content


# --- Entry Point --------------------------------------------------------------

EXAMPLE_TASK = (
    "List all files in /tmp, show disk usage with du -sh /tmp/*, "
    "and write a summary of what you find to /tmp/clive/summary.txt."
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LLM agent that drives CLI tools via tmux",
        epilog=(
            "Compose toolsets with +: -t standard+media+ai\n"
            "Categories: " + ", ".join(sorted(CATEGORIES.keys()))
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "task",
        nargs="?",
        default=EXAMPLE_TASK,
        help="Task for the agent to perform (default: built-in example)",
    )
    parser.add_argument(
        "-t", "--toolset",
        default=DEFAULT_TOOLSET,
        metavar="SPEC",
        help=(
            f"Toolset spec: profile name, category combo with +, or mix "
            f"(default: {DEFAULT_TOOLSET})"
        ),
    )
    parser.add_argument(
        "--list-toolsets",
        action="store_true",
        help="List available profiles and exit",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="List all tools across all three surfaces and exit",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch the interactive TUI instead of CLI mode",
    )
    args = parser.parse_args()

    if args.list_toolsets:
        profiles = list_toolsets()
        print("\nProfiles (use with -t):\n")
        for name, pane_names in profiles.items():
            cats = PROFILES.get(name, [])
            if isinstance(cats, list):
                cat_str = " + ".join(cats)
            else:
                cat_str = str(cats)
            marker = " (default)" if name == DEFAULT_TOOLSET else ""
            print(f"  {name:12s}{marker}")
            print(f"    panes:      {', '.join(pane_names)}")
            print(f"    categories: {cat_str}")
            print()

        print("Categories (compose with +):\n")
        cats = list_categories()
        for name, cat_def in sorted(cats.items()):
            parts = []
            if cat_def.get("panes"):
                parts.append(f"panes: {', '.join(cat_def['panes'])}")
            if cat_def.get("commands"):
                parts.append(f"commands: {', '.join(cat_def['commands'])}")
            if cat_def.get("endpoints"):
                parts.append(f"endpoints: {', '.join(cat_def['endpoints'])}")
            print(f"  {name:14s} {'; '.join(parts)}")
        print()
        raise SystemExit(0)

    if args.list_tools:
        resolved = resolve_toolset("full+images+dev+voice+ai+sync+translation+social+finance")
        available_cmds, missing_cmds = check_commands(resolved["commands"])

        print("\nPANES (conversation channels):\n")
        for p in resolved["panes"]:
            print(f"  {p['name']:16s} [{p['app_type']}]")
            print(f"    {p['description'][:80]}")
            if p.get("check"):
                print(f"    install: {p.get('install', '')}")
            print()

        print("COMMANDS (run in any shell pane):\n")
        for cmd in available_cmds:
            print(f"  + {cmd['name']:20s} {cmd['description']}")
        for cmd in missing_cmds:
            print(f"  - {cmd['name']:20s} {cmd['description']}")
            print(f"    {'':20s} install: {cmd.get('install', '')}")
        print()

        print("APIS (curl from any pane, always available):\n")
        for ep in resolved["endpoints"]:
            print(f"  * {ep['name']:20s} {ep['description']}")
            print(f"    {'':20s} {ep['usage']}")
        print()
        raise SystemExit(0)

    if args.tui:
        from tui import CliveApp
        CliveApp().run()
        raise SystemExit(0)

    run(args.task, toolset_spec=args.toolset)
