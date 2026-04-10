"""CLI subcommand handlers — extracted from clive.py's __main__ block.

Each function handles one `--<subcommand>` flag and raises SystemExit
when done. The dispatch table in clive.py picks the right one based on
the parsed argparse namespace.
"""

import os
import signal
import subprocess
import sys

from toolsets import (
    PROFILES,
    CATEGORIES,
    DEFAULT_TOOLSET,
    list_toolsets as _list_toolsets,
    list_categories,
    resolve_toolset,
    check_commands,
)
from config import find_config_schema, is_configured, run_setup


def handle_list_toolsets(args) -> None:
    profiles = _list_toolsets()
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


def handle_list_tools(args) -> None:
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


def handle_setup(args) -> None:
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


def handle_tui(args) -> None:
    from tui import CliveApp
    CliveApp().run()
    raise SystemExit(0)


def handle_undo(args) -> None:
    from selfmod.workspace import rollback, list_snapshots
    snaps = list_snapshots()
    if not snaps:
        print("No selfmod snapshots to undo.")
        raise SystemExit(0)
    tag = rollback()
    print(f"Rolled back to {tag}")
    raise SystemExit(0)


def handle_selfmod(args) -> None:
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


def handle_list_skills(args) -> None:
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


def handle_evolve(args) -> None:
    from evolve import evolve_driver
    evolve_driver(args.evolve)
    raise SystemExit(0)


def handle_list_schedules(args) -> None:
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


def handle_remove_schedule(args) -> None:
    from scheduler import remove_schedule
    if remove_schedule(args.remove_schedule):
        print(f"Removed schedule: {args.remove_schedule}")
    else:
        print(f"Schedule not found: {args.remove_schedule}")
    raise SystemExit(0)


def handle_pause_schedule(args) -> None:
    from scheduler import pause_schedule
    if pause_schedule(args.pause_schedule):
        print(f"Paused schedule: {args.pause_schedule}")
    else:
        print(f"Schedule not found: {args.pause_schedule}")
    raise SystemExit(0)


def handle_resume_schedule(args) -> None:
    from scheduler import resume_schedule
    if resume_schedule(args.resume_schedule):
        print(f"Resumed schedule: {args.resume_schedule}")
    else:
        print(f"Schedule not found: {args.resume_schedule}")
    raise SystemExit(0)


def handle_run_now(args) -> None:
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


def handle_history(args) -> None:
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


def handle_dashboard(args) -> None:
    from dashboard import render_snapshot
    render_snapshot()
    raise SystemExit(0)


def handle_stop(args) -> None:
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


def handle_instances(args) -> None:
    from server.discovery import discover_sessions, format_instances
    sessions = discover_sessions()
    print(format_instances(sessions))
    raise SystemExit(0)


def handle_status(args) -> None:
    import json
    health_path = os.path.expanduser("~/.clive/health.json")
    if os.path.exists(health_path):
        from server.health import format_health_dict
        with open(health_path) as f:
            health = json.load(f)
        print(format_health_dict(health))
    else:
        print("No server running (health file not found)")
    raise SystemExit(0)


def handle_serve(args) -> None:
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
