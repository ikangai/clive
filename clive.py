#!/usr/bin/env python3
"""
clive — CLI Live Environment.

tmux agent loop with planning, subtask decomposition, and parallel execution.

Usage:
    python clive.py "your task description"
    python clive.py -t standard "browse example.com and summarize it"
    python clive.py --list-toolsets
    python clive.py                          # uses built-in example task

Toolsets:
    minimal   — shell only (default, zero install)
    standard  — shell + browser + data + docs
    full      — standard + email + calendar + tasks + media
    remote    — shell + remote browser + remote files + email

    Watch in real-time:
        tmux attach -t clive

Requirements:
    pip install -r requirements.txt

Environment:
    OPENROUTER_API_KEY (set in .env file)
"""

import argparse
import time

from dotenv import load_dotenv

load_dotenv()

from session import setup_session, check_health, SESSION_NAME
from toolsets import get_toolset, list_toolsets, DEFAULT_TOOLSET
from planner import create_plan, display_plan
from executor import execute_plan
from models import SubtaskStatus
from llm import get_client, chat
from prompts import build_summarizer_prompt


def run(task: str, tools: list = None, toolset: str = DEFAULT_TOOLSET):
    if tools is None:
        tools = get_toolset(toolset)

    print(f"\n{'═' * 60}")
    print(f"Setting up session: {SESSION_NAME}")
    print(f"{'─' * 60}")

    session, panes = setup_session(tools)

    print(f"\nHealth check:")
    tool_status = check_health(panes)

    print(f"\n{'─' * 60}")
    print(f"Task: {task}")
    print(f"Watch: tmux attach -t {SESSION_NAME}")
    print(f"{'═' * 60}\n")

    start_time = time.time()

    # Phase 1: Planning
    print("Phase 1: Planning...")
    plan = create_plan(task, panes, tool_status)
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

    print(f"\n{'═' * 60}")
    print(f"TASK COMPLETE ({completed}/{total} subtasks succeeded)")
    print(f"{'═' * 60}")
    print(summary)
    print(f"{'─' * 60}")
    print(f"Time:   {elapsed:.1f}s")
    print(f"Tokens: {total_pt} prompt + {total_ct} completion = {total_pt + total_ct} total")
    print(f"{'═' * 60}\n")

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


# ─── Entry Point ──────────────────────────────────────────────────────────────

EXAMPLE_TASK = (
    "List all files in /tmp, show disk usage with du -sh /tmp/*, "
    "and write a summary of what you find to /tmp/clive/summary.txt."
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LLM agent that drives CLI tools via tmux"
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
        choices=list_toolsets().keys(),
        metavar="PROFILE",
        help=f"Tool profile to use (default: {DEFAULT_TOOLSET})",
    )
    parser.add_argument(
        "--list-toolsets",
        action="store_true",
        help="List available toolsets and exit",
    )
    args = parser.parse_args()

    if args.list_toolsets:
        profiles = list_toolsets()
        for name, tools in profiles.items():
            marker = " (default)" if name == DEFAULT_TOOLSET else ""
            print(f"  {name}{marker}: {', '.join(tools)}")
        raise SystemExit(0)

    run(args.task, toolset=args.toolset)
