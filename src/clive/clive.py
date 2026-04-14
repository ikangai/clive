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
    if args.agents_doctor: _ch.handle_agents_doctor(args)
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

    # ─── Special roles (rooms / lobby) ────────────────────────────────
    role = getattr(args, "role", None)
    if role == "lobby-client":
        from lobby_client import run as _lobby_client_run
        raise SystemExit(_lobby_client_run(
            socket_path=getattr(args, "lobby_socket", None)
        ))
    if role == "broker":
        from pathlib import Path as _Path
        from lobby_server import LobbyServer
        lobby_dir = _Path.home() / ".clive" / "lobby"
        # Restrictive dir perms defend the socket file from other
        # local users even if a future change widens the socket mode.
        lobby_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        sock = getattr(args, "lobby_socket", None) or str(lobby_dir / "lobby.sock")
        srv = LobbyServer(
            socket_path=sock,
            instance_name=getattr(args, "name", None) or "lobby",
        )
        try:
            srv.start()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1)
        try:
            srv.run_forever()
        except KeyboardInterrupt:
            srv.shutdown()
        raise SystemExit(0)

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
        from output import set_conversational, emit_turn, emit_context, emit_question, emit_alive
        set_conversational(True)

        # Named instances loop: run task(s), then wait for more on stdin
        keep_alive = bool(_instance_name)

        # Keepalive ticker: emit an `alive` frame every 15 seconds for
        # the entire lifetime of the conversational block. Lets the
        # outer (or any supervisor reading the pane) distinguish a
        # slow-but-working inner from a wedged one. Daemon thread so
        # SystemExit tears it down without lingering.
        import threading
        _alive_stop = threading.Event()

        def _alive_ticker():
            while not _alive_stop.is_set():
                try:
                    emit_alive()
                except Exception:
                    # stdout closed / broken pipe — outer is gone. No
                    # point in continuing to tick. The main thread will
                    # notice the EOF on its own stdin read.
                    return
                _alive_stop.wait(15.0)

        _alive_thread = threading.Thread(
            target=_alive_ticker, name="clive-alive-ticker", daemon=True,
        )
        _alive_thread.start()

        try:
            # Read task from args (SSH command) or — only in
            # non-keep-alive mode — one line from stdin. In keep-alive
            # mode we skip the initial readline and let the loop below
            # handle stdin, so that control words like "exit" / "quit"
            # / "/stop" work on the FIRST line the user sends, not just
            # on subsequent ones.
            task = args.task
            if not task and not keep_alive:
                try:
                    task = sys.stdin.readline().strip()
                except EOFError:
                    emit_turn("failed")
                    raise SystemExit(1)

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

            # Named instance: loop, waiting for tasks on stdin.
            # Uses the selectors-based ConvLoop (Phase 0) so later
            # phases can register the lobby pane reader alongside
            # stdin without another refactor. Behaviour parity with
            # the prior blocking-readline version: EOF exits, the
            # sentinel words (exit/quit//stop) exit, handler
            # exceptions emit failure frames but do NOT tear the
            # loop down.
            from conv_loop import ConvLoop

            def _handle_task_line(line: str) -> bool:
                task = line.strip()
                if not task:
                    return False
                if task.lower() in ("exit", "quit", "/stop"):
                    return True
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
                return False

            _conv_loop = ConvLoop()
            _conv_loop.on_line(sys.stdin, _handle_task_line)

            # ─── --join: attach to one or more room lobbies ───────
            # Parse each spec "room@lobby"; group rooms per lobby
            # so a single socket connection carries all rooms a
            # member wants on that lobby. Fails hard before entering
            # the loop if any resolution fails — no partial attach.
            _lobby_handles: list = []
            if getattr(args, "join", None):
                from lobby_connector import connect_local, ConnectError
                from room_participant import RoomParticipant
                from llm import get_client as _get_llm_client

                rooms_by_lobby: dict[str, list[str]] = {}
                for spec in args.join:
                    if "@" not in spec:
                        print(f"--join expects room@lobby, got {spec!r}",
                              file=sys.stderr)
                        raise SystemExit(2)
                    room, lobby = spec.split("@", 1)
                    rooms_by_lobby.setdefault(lobby, []).append(room)

                _member_name = _instance_name or "anonymous"
                try:
                    _llm_client = _get_llm_client()
                except Exception as e:
                    print(f"--join: cannot build LLM client: {e}",
                          file=sys.stderr)
                    raise SystemExit(1)

                for _lobby_name, _rooms in rooms_by_lobby.items():
                    try:
                        _sock, _nonce = connect_local(_lobby_name)
                    except ConnectError as e:
                        print(f"--join: {e}", file=sys.stderr)
                        raise SystemExit(1)
                    _participant = RoomParticipant(
                        name=_member_name,
                        nonce=_nonce,
                        llm_client=_llm_client,
                    )
                    # Bootstrap (blocking writes are safe: the socket
                    # is still blocking at this point).
                    for _frame in _participant.bootstrap(rooms=_rooms):
                        _sock.sendall((_frame + "\n").encode("utf-8"))

                    # Wire the socket as a line source on the
                    # ConvLoop. The ConvLoop will flip the fd to
                    # non-blocking for reads; writes still use
                    # sock.sendall (frames are small, local-socket
                    # sends fit in one syscall).
                    def _make_handler(p=_participant, s=_sock):
                        def _handler(line: str) -> bool:
                            for out in p.on_line(line):
                                try:
                                    s.sendall((out + "\n").encode("utf-8"))
                                except OSError:
                                    # Lobby connection lost; swallow
                                    # so the rest of the ConvLoop
                                    # (stdin) keeps running.
                                    return False
                            return False
                        return _handler

                    _conv_loop.on_line(_sock.fileno(), _make_handler())
                    _lobby_handles.append(_sock)

            try:
                _conv_loop.run()
            finally:
                for _s in _lobby_handles:
                    try:
                        _s.close()
                    except OSError:
                        pass
        finally:
            # Signal the ticker to stop. Daemon=True means it dies with
            # the process anyway, but signalling lets it exit its
            # blocking wait() promptly for a cleaner shutdown.
            _alive_stop.set()

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
        from cli_modes import run_repl
        run_repl(
            args,
            instance_name=_instance_name,
            output_format=output_format,
            register_fn=_register if _instance_name else None,
        )
        raise SystemExit(0)

    if args.dry_run:
        from cli_modes import run_dry_run
        run_dry_run(args)
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
