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
    from cli_args import build_parser
    args = build_parser().parse_args()

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

    # ─── One-shot subcommand dispatch ─────────────────────────────────
    import cli_handlers as _ch
    if args.list_toolsets: _ch.handle_list_toolsets(args)
    if args.list_tools: _ch.handle_list_tools(args)
    if args.setup: _ch.handle_setup(args)
    if args.tui: _ch.handle_tui(args)
    if args.safe_mode:
        os.environ["CLIVE_EXPERIMENTAL_SELFMOD"] = "0"
        print("Safe mode: self-modification disabled.")
    if args.undo: _ch.handle_undo(args)
    if args.selfmod: _ch.handle_selfmod(args)
    if args.list_skills: _ch.handle_list_skills(args)
    if args.evolve: _ch.handle_evolve(args)
    if args.list_schedules: _ch.handle_list_schedules(args)
    if args.remove_schedule: _ch.handle_remove_schedule(args)
    if args.pause_schedule: _ch.handle_pause_schedule(args)
    if args.resume_schedule: _ch.handle_resume_schedule(args)
    if args.run_now: _ch.handle_run_now(args)
    if args.history: _ch.handle_history(args)
    if args.dashboard: _ch.handle_dashboard(args)
    if args.stop: _ch.handle_stop(args)
    if args.instances: _ch.handle_instances(args)
    if args.status: _ch.handle_status(args)
    if args.serve: _ch.handle_serve(args)

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
