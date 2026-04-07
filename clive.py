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
import os
import re as _re
import signal
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from output import progress, result
from session import setup_session, check_health, generate_session_id, SESSION_NAME
from toolsets import (
    resolve_toolset, check_commands, build_tools_summary,
    print_availability, list_toolsets, list_categories,
    DEFAULT_TOOLSET, PROFILES, CATEGORIES,
)
from planner import create_plan, display_plan
from executor import execute_plan
from models import SubtaskStatus, Plan, Subtask
from llm import get_client, chat
from prompts import build_summarizer_prompt


TRIVIAL_PATTERNS = [
    _re.compile(r'^(list|count|find|show|cat|head|tail|wc|grep|ls|du|df)\b', _re.IGNORECASE),
    _re.compile(r'^(curl|wget|fetch)\s+\S+$', _re.IGNORECASE),
]

def _is_trivial(task: str, num_panes: int) -> bool:
    """Detect tasks that don't need planning."""
    if num_panes > 1:
        return False  # multi-pane tasks always need planning
    if len(task.split()) > 20:
        return False
    return any(p.search(task.strip()) for p in TRIVIAL_PATTERNS)


def run(task: str, toolset_spec: str = DEFAULT_TOOLSET, output_format: str = "default", max_tokens: int = 50000):
    session_id = generate_session_id()
    session_dir = f"/tmp/clive/{session_id}"

    # Graceful shutdown handler
    def _cleanup(signum=None, frame=None):
        import shutil
        progress("\nShutting down...")
        try:
            import libtmux
            server = libtmux.Server()
            for s in server.sessions.filter(session_name=SESSION_NAME):
                s.kill()
        except Exception:
            pass
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
        if signum:
            sys.exit(130)  # 128 + SIGINT

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    # Resolve toolset spec into three surfaces
    resolved = resolve_toolset(toolset_spec)

    progress(f"\n{'=' * 60}")
    progress(f"Setting up session: {SESSION_NAME}")
    progress(f"{'~' * 60}")

    # Create tmux session with pane tools only
    session, panes = setup_session(resolved["panes"], session_dir=session_dir)

    progress(f"\nHealth check:")
    tool_status = check_health(panes)

    # Auto-detect which commands are installed
    available_cmds, missing_cmds = check_commands(resolved["commands"])

    progress("")
    print_availability(
        tool_status, available_cmds, missing_cmds,
        resolved["endpoints"], resolved["categories"],
    )

    # Build enriched tools summary for the LLM (all three surfaces)
    tools_summary = build_tools_summary(
        tool_status, available_cmds, resolved["endpoints"],
    )

    progress(f"{'~' * 60}")
    progress(f"Task: {task}")
    progress(f"Session: {session_dir}")
    progress(f"Watch: tmux attach -t {SESSION_NAME}")
    progress(f"{'=' * 60}\n")

    start_time = time.time()

    # Fast path: skip planner for trivial single-pane tasks
    if _is_trivial(task, len(panes)):
        progress("Phase 1: Trivial task — skipping planner")
        first_pane = list(panes.keys())[0]
        plan = Plan(task=task, subtasks=[
            Subtask(id="1", description=task, pane=first_pane, mode="script"),
        ])
        display_plan(plan)
    else:
        progress("Phase 1: Planning...")
        plan = create_plan(task, panes, tool_status, tools_summary=tools_summary)
        display_plan(plan)

    # Phase 2: Execution
    progress("Phase 2: Executing...")

    def _progress_event(event_type, *args):
        """Print subtask completions as they happen."""
        if event_type == "subtask_done":
            sid, summary, elapsed = args
            progress(f"  ✓ [{sid}] {summary[:70]} ({elapsed:.1f}s)")
        elif event_type == "subtask_fail":
            sid, msg = args
            progress(f"  ✗ [{sid}] {msg[:70]}")

    results = execute_plan(plan, panes, tool_status, on_event=_progress_event, session_dir=session_dir, max_tokens=max_tokens)

    # Check for failures with skipped dependents → attempt replan
    failed = [r for r in results if r.status == SubtaskStatus.FAILED]
    skipped = [r for r in results if r.status == SubtaskStatus.SKIPPED]
    if failed and skipped and len(failed) <= 2:
        progress("\nReplanning: some subtasks failed, attempting recovery...")
        failure_context = "\n".join(
            f"  Subtask {r.subtask_id} FAILED: {r.summary}" for r in failed
        )
        remaining = "\n".join(
            f"  Subtask {r.subtask_id} SKIPPED: {r.summary}" for r in skipped
        )
        replan_task = (
            f"Original task: {task}\n\n"
            f"These subtasks failed:\n{failure_context}\n\n"
            f"These subtasks were skipped:\n{remaining}\n\n"
            f"Find an alternative approach to complete the remaining work. "
            f"Account for the failures — try a different method."
        )
        try:
            replan = create_plan(replan_task, panes, tool_status, tools_summary=tools_summary)
            if replan.subtasks:
                progress("Replanned — executing recovery subtasks...")
                recovery_results = execute_plan(replan, panes, tool_status, on_event=_progress_event, session_dir=session_dir, max_tokens=max_tokens // 2)
                results.extend(recovery_results)
        except Exception as e:
            progress(f"  Replan failed: {e}")

    # Phase 3: Summarization (skip for single-subtask plans — result IS the summary)
    if len(results) == 1 and results[0].status == SubtaskStatus.COMPLETED:
        progress("\nPhase 3: Single subtask — using result directly")
        summary = results[0].summary
    else:
        progress("\nPhase 3: Summarizing...")
        summary = _summarize(task, results, output_format=output_format)

    elapsed = time.time() - start_time
    total_pt = sum(r.prompt_tokens for r in results)
    total_ct = sum(r.completion_tokens for r in results)
    completed = sum(1 for r in results if r.status == SubtaskStatus.COMPLETED)
    total = len(results)

    progress(f"\n{'=' * 60}")
    progress(f"TASK COMPLETE ({completed}/{total} subtasks succeeded)")
    progress(f"{'=' * 60}")
    result(summary)
    progress(f"{'~' * 60}")
    progress(f"Time:   {elapsed:.1f}s")
    progress(f"Tokens: {total_pt} prompt + {total_ct} completion = {total_pt + total_ct} total")
    progress(f"{'=' * 60}\n")

    # Cleanup session directory (unless --keep-session)
    import shutil
    if os.path.isdir(session_dir) and not os.environ.get("CLIVE_KEEP_SESSION"):
        shutil.rmtree(session_dir, ignore_errors=True)

    return summary


def _summarize(task: str, results: list, output_format: str = "default") -> str:
    """Final LLM call to synthesize all subtask results."""
    client = get_client()

    result_text = "\n\n".join(
        f"Subtask {r.subtask_id} [{r.status.value}]: {r.summary}"
        for r in results
    )

    messages = [
        {"role": "system", "content": build_summarizer_prompt(output_format)},
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
    parser.add_argument(
        "--selfmod",
        metavar="GOAL",
        help="Self-modify clive (experimental, requires CLIVE_EXPERIMENTAL_SELFMOD=1)",
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="Roll back last self-modification",
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help="Disable self-modification for this run",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Quiet mode: telemetry to stderr, only result to stdout",
    )
    parser.add_argument("--oneline", action="store_true", help="Single-line result output")
    parser.add_argument("--bool", action="store_true", help="Yes/No output, exit 0=yes 1=no")
    parser.add_argument("--json", action="store_true", help="Structured JSON result output")
    parser.add_argument(
        "--evolve",
        metavar="DRIVER",
        help="Evolve a driver prompt (shell, browser, all)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging to stderr",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=50000,
        help="Maximum total tokens before aborting (default: 50000)",
    )
    args = parser.parse_args()

    # Configure logging
    import logging
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            stream=sys.stderr,
        )
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

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
        all_cats = "+".join(sorted(CATEGORIES.keys()))
        resolved = resolve_toolset(all_cats)
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

    if args.safe_mode:
        import os
        os.environ["CLIVE_EXPERIMENTAL_SELFMOD"] = "0"
        print("Safe mode: self-modification disabled.")

    if args.undo:
        from selfmod.workspace import rollback, list_snapshots
        snaps = list_snapshots()
        if not snaps:
            print("No selfmod snapshots to undo.")
            raise SystemExit(0)
        tag = rollback()
        print(f"Rolled back to {tag}")
        raise SystemExit(0)

    if args.selfmod:
        from selfmod import is_enabled
        from selfmod.pipeline import run_pipeline

        if not is_enabled():
            print("Self-modification is disabled.")
            print("Set CLIVE_EXPERIMENTAL_SELFMOD=1 in .env to enable.")
            raise SystemExit(1)

        def _cli_status(stage: str, msg: str) -> None:
            print(f"  [{stage}] {msg}")

        result = run_pipeline(args.selfmod, on_status=_cli_status)
        if result.success:
            print(f"\n✓ Applied: {result.message}")
            print(f"  Snapshot: {result.snapshot_tag}")
            print(f"  Tokens: {result.tokens['prompt'] + result.tokens['completion']:,}")
            print("  Use --undo to roll back.")
        else:
            print(f"\n✗ {result.stage}: {result.message}")
            raise SystemExit(1)
        raise SystemExit(0)

    if args.evolve:
        from evolve import evolve_driver
        evolve_driver(args.evolve)
        raise SystemExit(0)

    output_format = "default"
    if args.oneline:
        output_format = "oneline"
        from output import set_quiet
        set_quiet(True)
    elif args.bool:
        output_format = "bool"
        from output import set_quiet
        set_quiet(True)
    elif args.json:
        output_format = "json"
        from output import set_quiet
        set_quiet(True)

    if args.quiet:
        from output import set_quiet
        set_quiet(True)

    summary = run(args.task, toolset_spec=args.toolset, output_format=output_format, max_tokens=args.max_tokens)

    if args.bool:
        import sys
        sys.exit(0 if summary.strip().upper().startswith("YES") else 1)
