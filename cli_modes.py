"""Long-form mode handlers extracted from clive.py __main__.

Contains the interactive REPL and the `--dry-run` planner preview.
Keeps clive.py focused on arg parsing and top-level mode dispatch.
"""
import os
import shutil

import libtmux

from llm import PROVIDER_NAME, MODEL
from session import generate_session_id, SESSION_NAME, SOCKET_NAME, setup_session, check_health
from output import step, detail, progress
from toolsets import CATEGORIES, resolve_toolset, check_commands, build_tools_summary
from models import Plan, Subtask
from planner import create_plan, display_plan
from clive_core import run, _setup_session, _expand_toolset


def run_repl(args, instance_name=None, output_format="default", register_fn=None):
    """Run the interactive REPL loop until the user exits.

    Parameters
    ----------
    args : argparse.Namespace
        Must provide ``toolset``, ``max_tokens``, ``task`` attributes.
    instance_name : str | None
        If set, register this instance via ``register_fn`` once the tmux
        session name is known.
    output_format : str
        One of ``default``, ``oneline``, ``json``, ``bool``.
    register_fn : callable | None
        Callable matching ``registry.register``. Only used when
        ``instance_name`` is set.
    """
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

    session_id = generate_session_id()
    session_dir = f"/tmp/clive/{session_id}"
    repl_state = {"session_name": SESSION_NAME}
    session_ctx = _setup_session(args.toolset, session_dir, repl_state)

    if instance_name and register_fn:
        register_fn(instance_name, pid=os.getpid(),
                    tmux_session=repl_state["session_name"],
                    tmux_socket="clive", toolset=args.toolset,
                    task=args.task or "", conversational=True,
                    session_dir=session_dir)

    def _cleanup():
        try:
            server = libtmux.Server(socket_name=SOCKET_NAME)
            for s in server.sessions.filter(session_name=repl_state["session_name"]):
                s.kill()
        except Exception:
            pass
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)

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
        _cleanup()


def run_dry_run(args):
    """Plan-only mode: resolve toolset, create plan, display it, then clean up.

    Note: references a module-level ``_is_trivial`` name that is not
    currently defined (pre-existing latent bug preserved verbatim from
    clive.py). Calling this function with a trivial task will raise
    NameError — matching the behavior prior to extraction.
    """
    if not args.task:
        import sys
        print("Error: --dry-run requires a task argument.", file=sys.stderr)
        raise SystemExit(1)
    resolved = resolve_toolset(args.toolset)
    session, panes, dry_session_name = setup_session(resolved["panes"], session_dir="/tmp/clive/dryrun")
    available_cmds, _ = check_commands(resolved["commands"])
    tools_summary = build_tools_summary(
        check_health(panes), available_cmds, resolved["endpoints"],
    )
    if _is_trivial(args.task, len(panes)):  # noqa: F821 (pre-existing latent bug)
        first_pane = list(panes.keys())[0]
        plan = Plan(task=args.task, subtasks=[
            Subtask(id="1", description=args.task, pane=first_pane, mode="script"),
        ])
    else:
        plan = create_plan(args.task, panes, check_health(panes), tools_summary=tools_summary)
    display_plan(plan)
    print(f"\n(dry run — {len(plan.subtasks)} subtask(s), not executed)")
    try:
        server = libtmux.Server(socket_name=SOCKET_NAME)
        for s in server.sessions.filter(session_name=dry_session_name):
            s.kill()
    except Exception:
        pass
    shutil.rmtree("/tmp/clive/dryrun", ignore_errors=True)
