#!/usr/bin/env python3
"""
tmux Agent Loop — v1 with planning, subtask decomposition, and parallel execution.

Usage:
    python agent.py "your task description"
    python agent.py                          # uses built-in example task

    Watch in real-time:
        tmux attach -t agent

Requirements:
    pip install -r requirements.txt

Environment:
    OPENROUTER_API_KEY (set in .env file)
"""

import argparse
import time

from dotenv import load_dotenv

load_dotenv()

from session import setup_session, check_health, DEFAULT_TOOLS, SESSION_NAME
from planner import create_plan, display_plan
from executor import execute_plan
from models import SubtaskStatus
from llm import get_client, chat
from prompts import build_summarizer_prompt


def run(task: str, tools: list = DEFAULT_TOOLS):

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
    "In the browser pane: fetch https://example.com using lynx -dump "
    "and save the output to /tmp/agent/example.txt. "
    "Then check the links in the file read the content and update the file. "
    "After you went through all links summarize what you found."
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
    args = parser.parse_args()

    run(args.task)
