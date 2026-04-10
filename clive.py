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
import json
import os
import re as _re
import shutil
import signal
import sys
import time
from difflib import SequenceMatcher

import libtmux
from dotenv import load_dotenv

load_dotenv()

from output import progress, step, detail, activity, finish, result
from session import (
    setup_session, check_health, generate_session_id, add_pane,
    SESSION_NAME, SOCKET_NAME,
)
from toolsets import (
    resolve_toolset, check_commands, build_tools_summary,
    print_availability, list_toolsets, list_categories,
    find_category, normalize_tool_name,
    DEFAULT_TOOLSET, PROFILES, CATEGORIES, PANES, COMMANDS, ENDPOINTS,
)
from planner import create_plan, display_plan
from executor import execute_plan, cancel as cancel_executor, reset_cancel, is_cancelled
from router import route_task
from models import SubtaskStatus, Plan, Subtask, ClassifierResult
from llm import get_client, chat, CLASSIFIER_MODEL, PROVIDER_NAME, MODEL
from prompts import build_summarizer_prompt, build_classifier_prompt
from config import get_unconfigured, run_setup, find_config_schema, is_configured
import summarizer

# Runtime helpers (routing, session setup, run loop) live in clive_core.
# Re-export _is_direct for tests that `from clive import _is_direct`.
from clive_core import (
    run,
    _setup_session,
    _expand_toolset,
    _is_direct,
)


# --- Entry Point --------------------------------------------------------------

EXAMPLE_TASK = (
    "List all files in /tmp, show disk usage with du -sh /tmp/*, "
    "and write a summary of what you find to /tmp/clive/summary.txt."
)

if __name__ == "__main__":
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
    parser.add_argument(
        "task",
        nargs="?",
        default=None,
        help="Task for the agent to perform",
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
        "--conversational",
        action="store_true",
        help="Conversational mode for clive-to-clive peer dialogue (auto-detected via isatty)",
    )
    parser.add_argument(
        "--list-skills",
        action="store_true",
        help="List available skills",
    )
    parser.add_argument(
        "--evolve",
        metavar="DRIVER",
        help="Evolve a driver prompt (shell, browser, all)",
    )
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the execution plan without running it",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="clive 0.2.0",
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

    # Pre-flight checks
    if not shutil.which("tmux"):
        print("Error: tmux not found. Install it first:", file=sys.stderr)
        print("  macOS:  brew install tmux", file=sys.stderr)
        print("  Ubuntu: sudo apt install tmux", file=sys.stderr)
        raise SystemExit(1)

    # Check API key early (skip for list/version commands)
    from llm import _provider, PROVIDER_NAME
    _api_key_env = _provider.get("api_key_env")
    if _api_key_env and not os.environ.get(_api_key_env):
        if not any(getattr(args, f, False) for f in ["list_toolsets", "list_tools", "list_skills", "list_schedules"]):
            print(f"Error: {_api_key_env} not set (required for {PROVIDER_NAME} provider)", file=sys.stderr)
            print(f"  Set it in .env or export {_api_key_env}=your-key", file=sys.stderr)
            raise SystemExit(1)

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
            cfg = p.get("config")
            if cfg:
                configured = is_configured(cfg)
                icon = "\u2713" if configured else "\u26a0"
                status = "configured" if configured else "needs setup"
                print(f"  {p['name']:16s} [{p['app_type']}] {icon} {status}")
            else:
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

    if args.setup:
        tool_name = args.setup
        config_schema = find_config_schema(tool_name)
        if not config_schema:
            print(f"No configuration needed for '{tool_name}'.")
            raise SystemExit(1)
        if is_configured(config_schema):
            print(f"'{tool_name}' is already configured.")
            reconfigure = input("Reconfigure? [y/N]: ").strip().lower()
            if reconfigure not in ("y", "yes"):
                raise SystemExit(0)
        run_setup(tool_name, config_schema)
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

    if args.list_skills:
        from skills import list_skills
        skills = list_skills()
        if skills:
            print("\nAvailable skills:\n")
            for s in skills:
                print(f"  {s['name']:20s} {s['description']}")
            print(f"\nUsage: include [skill:name] in your task description")
        else:
            print("No skills found in skills/ directory")
        raise SystemExit(0)

    if args.evolve:
        from evolve import evolve_driver
        evolve_driver(args.evolve)
        raise SystemExit(0)

    if args.list_schedules:
        from scheduler import list_schedules
        schedules = list_schedules()
        if schedules:
            print("\nScheduled tasks:\n")
            for s in schedules:
                status = "active" if s.get("active") else "paused"
                health = s.get("health", {})
                rate = health.get("success_rate", 0)
                streak = health.get("failure_streak", 0)
                health_str = f"{rate:.0%} ok" if health.get("runs") else "no runs"
                if streak >= 3:
                    health_str += f" ⚠ {streak} failures in a row"
                print(f"  {s['name']:20s} {s['cron']:15s} [{status:6s}] [{health_str:15s}] {s['task'][:40]}")
        else:
            print("No scheduled tasks. Use --schedule to add one.")
        raise SystemExit(0)

    if args.remove_schedule:
        from scheduler import remove_schedule
        if remove_schedule(args.remove_schedule):
            print(f"Removed schedule: {args.remove_schedule}")
        else:
            print(f"Schedule not found: {args.remove_schedule}")
        raise SystemExit(0)

    if args.pause_schedule:
        from scheduler import pause_schedule
        if pause_schedule(args.pause_schedule):
            print(f"Paused schedule: {args.pause_schedule}")
        else:
            print(f"Schedule not found: {args.pause_schedule}")
        raise SystemExit(0)

    if args.resume_schedule:
        from scheduler import resume_schedule
        if resume_schedule(args.resume_schedule):
            print(f"Resumed schedule: {args.resume_schedule}")
        else:
            print(f"Schedule not found: {args.resume_schedule}")
        raise SystemExit(0)

    if args.run_now:
        from scheduler import run_now
        print(f"Running {args.run_now} now...")
        try:
            result = run_now(args.run_now)
            status = result.get("status", "?")
            duration = result.get("duration_seconds", "?")
            print(f"  Status: {status}")
            print(f"  Duration: {duration}s")
            res = result.get("result", "")
            if res:
                print(f"  Result: {str(res)[:200]}")
        except FileNotFoundError:
            print(f"Schedule not found: {args.run_now}")
        except subprocess.TimeoutExpired:
            print(f"Timed out (300s limit)")
        raise SystemExit(0)

    if args.history:
        from scheduler import get_history
        history = get_history(args.history)
        if history:
            print(f"\nRun history for {args.history}:\n")
            for h in history:
                status = h.get("status", "?")
                ts = h.get("timestamp", "?")
                dur = h.get("duration_seconds", "?")
                res = str(h.get("result", ""))[:60]
                indicator = "✓" if status == "success" else "✗"
                print(f"  {indicator} {ts:20s} {status:8s} {dur:>4s}s  {res}")
        else:
            print(f"No history for {args.history}")
        raise SystemExit(0)

    if args.dashboard:
        from dashboard import render_snapshot
        render_snapshot()
        raise SystemExit(0)

    if args.stop:
        from registry import get_instance as _get_inst
        inst = _get_inst(args.stop)
        if inst is None:
            print(f"Instance '{args.stop}' not found or not running", file=sys.stderr)
            raise SystemExit(1)
        pid = inst["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to instance '{args.stop}' (PID {pid})")
        except OSError as e:
            print(f"Failed to stop '{args.stop}': {e}", file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(0)

    if args.instances:
        from server.discovery import discover_sessions, format_instances
        sessions = discover_sessions()
        print(format_instances(sessions))
        raise SystemExit(0)

    if args.status:
        health_path = os.path.expanduser("~/.clive/health.json")
        if os.path.exists(health_path):
            import json as json
            from server.health import format_health_dict
            with open(health_path) as f:
                health = json.load(f)
            print(format_health_dict(health))
        else:
            print("No server running (health file not found)")
        raise SystemExit(0)

    if args.serve:
        from server.supervisor import Supervisor

        print(f"Starting clive server with {args.workers} workers")
        print(f"Queue directory: {args.queue_dir}")

        sv = Supervisor(
            queue_dir=args.queue_dir,
            num_workers=args.workers,
            health_path=os.path.expanduser("~/.clive/health.json"),
            dry_run=args.dry_run,
        )
        try:
            sv.run()
        except KeyboardInterrupt:
            print("\nShutting down...")
            sv.shutdown()
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

    if args.remote:
        from remote import build_remote_command, check_remote_clive
        import subprocess as _sp

        host = args.remote
        step(f"Connecting to {host}")

        # Check remote availability
        check = check_remote_clive(host)
        if not check["available"]:
            detail(f"Warning: could not verify clive on {host}")

        # Build and execute remote command (no shell=True — prevents injection)
        remote_cmd = build_remote_command(args.task, toolset=args.toolset)
        ssh_args = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            host, f"cd ~ && {remote_cmd}",
        ]
        step(f"Running remote task")

        try:
            proc = _sp.run(ssh_args, capture_output=True, text=True, timeout=300)
            if proc.returncode == 0:
                result(proc.stdout.strip())
            else:
                step(f"Remote task failed (exit {proc.returncode})")
                if proc.stderr:
                    detail(f"stderr: {proc.stderr[:200]}")
                result(proc.stdout.strip() if proc.stdout else "Remote task failed")
        except _sp.TimeoutExpired:
            step("Remote task timed out (300s)")
        raise SystemExit(proc.returncode if 'proc' in dir() else 1)

    # Named instance: register, check collision, set up deregister on exit
    _instance_name = getattr(args, 'name', None)
    if _instance_name:
        from registry import is_name_available, register as _register, deregister as _deregister, get_instance as _get_inst_reg
        if not is_name_available(_instance_name):
            existing = _get_inst_reg(_instance_name)
            pid = existing["pid"] if existing else "?"
            print(f"Instance '{_instance_name}' is already running (PID {pid})", file=sys.stderr)
            raise SystemExit(1)

        import atexit
        def _deregister_on_exit():
            _deregister(_instance_name)
        atexit.register(_deregister_on_exit)

    # ─── Mode auto-detection ──────────────────────────────────────────
    # Conversational mode: explicit flag or no TTY (clive-to-clive via SSH)
    if args.conversational or (
        not sys.stdin.isatty()
        and not args.quiet
        and not args.json
        and not args.oneline
        and not args.bool
        and args.task
    ):
        from output import set_conversational, emit_turn, emit_context, emit_question
        set_conversational(True)

        # Named instances loop: run task(s), then wait for more on stdin
        keep_alive = bool(_instance_name)

        # Read task from args (SSH command) or stdin
        task = args.task
        if not task:
            try:
                task = sys.stdin.readline().strip()
            except EOFError:
                if not keep_alive:
                    emit_turn("failed")
                    raise SystemExit(1)
                task = None

        if task:
            emit_turn("thinking")
            try:
                summary = run(
                    task,
                    toolset_spec=args.toolset,
                    output_format="default",
                    max_tokens=args.max_tokens,
                )
                emit_context({"result": summary})
                emit_turn("done")
            except Exception as e:
                emit_context({"error": str(e)})
                emit_turn("failed")
                if not keep_alive:
                    raise SystemExit(1)
        elif not keep_alive:
            emit_context({"error": "No task provided"})
            emit_turn("failed")
            raise SystemExit(1)

        if not keep_alive:
            raise SystemExit(0)

        # Named instance: loop, waiting for tasks on stdin
        while True:
            try:
                line = sys.stdin.readline()
            except EOFError:
                break
            if not line:  # EOF
                break
            task = line.strip()
            if not task:
                continue
            if task.lower() in ("exit", "quit", "/stop"):
                break
            emit_turn("thinking")
            try:
                summary = run(
                    task,
                    toolset_spec=args.toolset,
                    output_format="default",
                    max_tokens=args.max_tokens,
                )
                emit_context({"result": summary})
                emit_turn("done")
            except Exception as e:
                emit_context({"error": str(e)})
                emit_turn("failed")

        raise SystemExit(0)

    if args.schedule:
        from scheduler import add_schedule
        entry = add_schedule(
            args.task, args.schedule,
            notify=args.notify,
            toolset=args.toolset,
        )
        print(f"Scheduled: {entry['name']}")
        print(f"  Task: {entry['task']}")
        print(f"  Cron: {entry['cron']}")
        print(f"  Toolset: {entry.get('toolset', 'minimal')}")
        if entry.get("notify"):
            print(f"  Notify: {entry['notify']}")
        print(f"  Results: ~/.clive/results/{entry['name']}/")
        raise SystemExit(0)

    # Interactive REPL mode: no task arg → show banner, set up session once, loop
    if not args.task and not args.dry_run:
        from llm import PROVIDER_NAME, MODEL
        print(f"""
 ██████╗██╗     ██╗██╗   ██╗███████╗
██╔════╝██║     ██║██║   ██║██╔════╝
██║     ██║     ██║██║   ██║█████╗
██║     ██║     ██║╚██╗ ██╔╝██╔══╝
╚██████╗███████╗██║ ╚████╔╝ ███████╗
 ╚═════╝╚══════╝╚═╝  ╚═══╝  ╚══════╝
  {MODEL} · {PROVIDER_NAME}
  toolset: {args.toolset}
""")

        # Set up session once for the REPL
        session_id = generate_session_id()
        session_dir = f"/tmp/clive/{session_id}"
        _repl_state = {"session_name": SESSION_NAME}
        session_ctx = _setup_session(args.toolset, session_dir, _repl_state)

        # Register named instance now that we have the session name
        if _instance_name:
            _register(_instance_name, pid=os.getpid(),
                      tmux_session=_repl_state["session_name"],
                      tmux_socket="clive", toolset=args.toolset,
                      task=args.task or "", conversational=True,
                      session_dir=session_dir)

        def _repl_cleanup():
            try:
                server = libtmux.Server(socket_name=SOCKET_NAME)
                for s in server.sessions.filter(session_name=_repl_state["session_name"]):
                    s.kill()
            except Exception:
                pass
            if os.path.isdir(session_dir):
                shutil.rmtree(session_dir, ignore_errors=True)

        # Enable readline for arrow keys, history, and special chars
        import readline
        # macOS libedit: don't steal Option key (needed for @ on German keyboards etc.)
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind -e")
            readline.parse_and_bind("bind '\\e[A' ed-search-prev-history")
            readline.parse_and_bind("bind '\\e[B' ed-search-next-history")
        else:
            readline.parse_and_bind("set enable-meta-key off")
        history_file = os.path.expanduser("~/.clive/history")
        os.makedirs(os.path.dirname(history_file), exist_ok=True)
        try:
            readline.read_history_file(history_file)
        except FileNotFoundError:
            pass
        readline.set_history_length(500)

        try:
            while True:
                try:
                    task = input("\nEnter task: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not task or task.lower() in ("exit", "quit", "q"):
                    break
                if task == "/dashboard":
                    from dashboard import render_lines
                    for line in render_lines():
                        print(line)
                    continue
                if task.startswith("/add "):
                    cat = task[5:].strip()
                    if cat in CATEGORIES:
                        if _expand_toolset(cat, session_ctx):
                            step(f"Added {cat}")
                        else:
                            detail(f"{cat} already loaded")
                    else:
                        detail(f"Unknown category: {cat}. Available: {', '.join(sorted(CATEGORIES.keys()))}")
                    continue
                if task == "/tools":
                    cats = sorted(session_ctx.get("categories", set()))
                    detail(f"Active: {', '.join(cats)}")
                    detail(f"Panes: {', '.join(session_ctx['panes'].keys())}")
                    avail = [c['name'] for c in session_ctx['available_cmds']]
                    if avail:
                        detail(f"Commands: {', '.join(avail)}")
                    uncfg = session_ctx.get('unconfigured', [])
                    if uncfg:
                        detail(f"Needs setup: {', '.join(uncfg)}")
                    continue
                try:
                    run(task, toolset_spec=args.toolset, output_format=output_format,
                        max_tokens=args.max_tokens, session_ctx=session_ctx, session_dir=session_dir)
                except (SystemExit, KeyboardInterrupt):
                    pass  # don't exit the REPL on Ctrl-C during a task
                except Exception as e:
                    progress(f"Error: {e}")
        finally:
            try:
                readline.write_history_file(history_file)
            except OSError:
                pass
            _repl_cleanup()

        raise SystemExit(0)

    if args.dry_run:
        if not args.task:
            print("Error: --dry-run requires a task argument.", file=sys.stderr)
            raise SystemExit(1)
        resolved = resolve_toolset(args.toolset)
        session, panes, dry_session_name = setup_session(resolved["panes"], session_dir="/tmp/clive/dryrun")
        available_cmds, _ = check_commands(resolved["commands"])
        tools_summary = build_tools_summary(
            check_health(panes), available_cmds, resolved["endpoints"],
        )
        if _is_trivial(args.task, len(panes)):
            first_pane = list(panes.keys())[0]
            plan = Plan(task=args.task, subtasks=[
                Subtask(id="1", description=args.task, pane=first_pane, mode="script"),
            ])
        else:
            plan = create_plan(args.task, panes, check_health(panes), tools_summary=tools_summary)
        display_plan(plan)
        print(f"\n(dry run — {len(plan.subtasks)} subtask(s), not executed)")
        # Cleanup
        try:
            server = libtmux.Server(socket_name=SOCKET_NAME)
            for s in server.sessions.filter(session_name=dry_session_name):
                s.kill()
        except Exception:
            pass
        shutil.rmtree("/tmp/clive/dryrun", ignore_errors=True)
        raise SystemExit(0)

    # Register named instance for single-task path
    if _instance_name:
        _register(_instance_name, pid=os.getpid(),
                  tmux_session=f"clive-{_instance_name}",
                  tmux_socket="clive", toolset=args.toolset,
                  task=args.task, conversational=True,
                  session_dir=f"/tmp/clive/{_instance_name}")

    summary = run(args.task, toolset_spec=args.toolset, output_format=output_format, max_tokens=args.max_tokens)

    if args.bool:
        import sys
        sys.exit(0 if summary.strip().upper().startswith("YES") else 1)
