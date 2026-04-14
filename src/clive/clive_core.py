"""clive core — runtime helpers extracted from clive.py.

Contains the routing callbacks (_is_direct / _classify / _find_cached_plan),
session setup (_setup_session, _expand_toolset), progress/output helpers,
the main `run()` entry, and `_run_inner` (Plan → Execute → Summarize).
The argparse + CLI dispatch layer stays in clive.py and imports from here.
"""

import json
import os
import re as _re
import shutil
import signal
import sys
import time
from difflib import SequenceMatcher

import libtmux

from output import progress, step, detail, activity, result
from session import (
    setup_session, check_health, generate_session_id, add_pane,
    SESSION_NAME, SOCKET_NAME,
)
from toolsets import (
    resolve_toolset, check_commands, build_tools_summary,
    DEFAULT_TOOLSET, CATEGORIES, PANES, COMMANDS, ENDPOINTS,
)
from executor import execute_plan, cancel as cancel_executor, reset_cancel, is_cancelled
from router import route_task
from models import SubtaskStatus, Plan, Subtask, ClassifierResult
from llm import get_client, chat, CLASSIFIER_MODEL, PROVIDER_NAME, MODEL
from prompts import build_classifier_prompt
from config import get_unconfigured
import summarizer


# ─── Tier 0: Regex-based direct command detection ────────────────────────────

_DIRECT_CMD_PATTERN = _re.compile(
    r'^(curl|wget|ls|cat|head|tail|wc|grep|find|du|df|stat|file|uname|date|whoami|hostname|pwd|id|echo|ping|dig|nslookup|traceroute|uptime|free|top|ps|env|printenv|sw_vers|system_profiler|sysctl|diskutil|ifconfig|ip|netstat|ss|lsof|mount|which|where|type|realpath|readlink|basename|dirname|sort|uniq|cut|tr|awk|sed|jq|python3?|ruby|node|perl|rg)\b',
    _re.IGNORECASE,
)

def _is_direct(task: str, num_panes: int) -> bool:
    """Tier 0: Detect tasks that are literal shell commands — no LLM needed."""
    if num_panes > 1:
        return False
    t = task.strip()
    if any(t.lower().startswith(w) for w in ("what ", "how ", "why ", "show me ", "list all ", "find the ", "check ")):
        return False
    return bool(_DIRECT_CMD_PATTERN.match(t))


# ─── Session context: files on disk + recent task history ───────────────────

_MAX_FILE_LISTING = 20
_MAX_HISTORY_ENTRIES = 5


def _render_session_files(session_dir: str | None) -> str | None:
    """List user-created files in the session dir in a form the planner/classifier can use."""
    if not session_dir or not os.path.isdir(session_dir):
        return None
    try:
        from file_inspect import format_file_context, sniff_session_files
    except ImportError:
        return None
    # Pass a subtask_id that cannot appear in real filenames so internal
    # files (leading underscore) are filtered out even without per-subtask matching.
    files = sniff_session_files(session_dir, subtask_id="__CLIVE_NO_MATCH__")
    files = [f for f in files if not os.path.basename(f.get("path", "")).startswith("_")]
    if not files:
        return None
    formatted = format_file_context(files[:_MAX_FILE_LISTING])
    # `format_file_context` prefixes with "Available files:\n" — strip it for a tidier block.
    return formatted.replace("Available files:\n", "", 1)


def _render_recent_history(history) -> str | None:
    """Render the last N (task, summary, files) entries as a compact scannable block."""
    if not history:
        return None
    entries = list(history)[-_MAX_HISTORY_ENTRIES:]
    if not entries:
        return None
    lines = []
    for i, entry in enumerate(entries, 1):
        task = (entry.get("task") or "").strip().replace("\n", " ")[:120]
        summary = (entry.get("summary") or "").strip().replace("\n", " ")[:160]
        files = entry.get("files") or []
        file_hint = f"  files: {', '.join(files[:4])}" if files else ""
        lines.append(f"  {i}. task: {task}\n     result: {summary}{file_hint}")
    return "\n".join(lines)


# ─── Tier 1: Fast LLM classifier ─────────────────────────────────────────────

def _classify(task: str, session_ctx: dict) -> ClassifierResult | None:
    """Tier 1: Use a fast/cheap model to classify intent and route."""
    if CLASSIFIER_MODEL == "none":
        return None

    panes = session_ctx["panes"]
    available_cmds = session_ctx.get("available_cmds", [])
    missing_cmds = session_ctx.get("missing_cmds", [])
    endpoints = session_ctx.get("endpoints", [])

    available_panes = list(panes.keys())
    installed = [cmd["name"] for cmd in available_cmds]
    missing = [cmd["name"] for cmd in missing_cmds]
    endpoint_names = [ep["name"] for ep in endpoints]
    unconfigured = session_ctx.get("unconfigured", [])

    system_prompt = build_classifier_prompt(
        available_panes=available_panes,
        installed_commands=installed,
        missing_commands=missing,
        available_endpoints=endpoint_names,
        unconfigured_tools=unconfigured,
        session_files=_render_session_files(session_ctx.get("session_dir")),
        recent_history=_render_recent_history(session_ctx.get("history")),
    )

    client = get_client()
    try:
        reply, pt, ct = chat(client, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ], max_tokens=256, model=CLASSIFIER_MODEL)

        # Parse JSON from reply; strip markdown fences if present
        text = reply.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()

        data = json.loads(text)
        detail(f"Classifier: {pt + ct} tokens, mode={data.get('mode')}")
        return ClassifierResult(
            mode=data.get("mode", "script"),
            tool=data.get("tool"),
            pane=data.get("pane"),
            driver=data.get("driver"),
            cmd=data.get("cmd"),
            fallback_mode=data.get("fallback_mode"),
            stateful=data.get("stateful", False),
            message=data.get("message"),
        )
    except Exception as e:
        detail(f"Classifier failed ({e}), falling back to planner")
        return None


def _find_cached_plan(task: str, panes: dict) -> Plan | None:
    """Look up session log for a similar successful plan to reuse."""
    if not os.path.exists(summarizer.SESSION_LOG):
        return None
    try:
        # Read last 50 entries
        with open(summarizer.SESSION_LOG, "r") as f:
            entries = [json.loads(l) for l in f.readlines()[-50:] if l.strip()]

        # Find entries with high similarity (SequenceMatcher is more robust than word overlap)
        for entry in reversed(entries):
            if entry.get("failed", 0) > 0:
                continue  # skip failed plans
            if entry.get("completed", 0) == 0:
                continue
            prev_task = entry.get("task", "")
            if not prev_task:
                continue
            similarity = SequenceMatcher(None, task.lower(), prev_task.lower()).ratio()
            if similarity > 0.65:
                # Reconstruct plan from cached shape (use rich steps if available)
                steps = entry.get("steps")
                pane_names = list(panes.keys())
                subtasks = []
                if steps:
                    for i, step_ in enumerate(steps):
                        pane = step_["pane"] if step_["pane"] in panes else pane_names[0]
                        subtasks.append(Subtask(
                            id=str(i + 1),
                            description=step_["desc"],
                            pane=pane,
                            mode=step_["mode"],
                            depends_on=[str(i)] if i > 0 else [],
                        ))
                else:
                    modes = entry.get("modes", ["script"])
                    for i, mode in enumerate(modes):
                        pane = pane_names[0] if len(pane_names) == 1 else pane_names[min(i, len(pane_names)-1)]
                        subtasks.append(Subtask(
                            id=str(i + 1),
                            description=task if len(modes) == 1 else f"Step {i+1} of: {task}",
                            pane=pane,
                            mode=mode,
                            depends_on=[str(i)] if i > 0 else [],
                        ))
                return Plan(task=task, subtasks=subtasks)
    except Exception:
        pass
    return None


def run(task: str, toolset_spec: str = DEFAULT_TOOLSET, output_format: str = "default", max_tokens: int = 50000, session_ctx=None, session_dir=None):
    reset_cancel()

    owns_session = session_ctx is None
    if session_dir is None:
        session_id = generate_session_id()
        session_dir = f"/tmp/clive/{session_id}"

    # Mutable state shared with _cleanup (updated once session is created)
    _state = {"session_name": SESSION_NAME}

    # Graceful shutdown handler
    def _cleanup(signum=None, frame=None):
        if signum:
            # Signal workers to stop first, then clean up
            cancel_executor()
            progress("\nCancelling...")
        else:
            progress("\nShutting down...")
        if owns_session:
            try:
                server = libtmux.Server(socket_name=SOCKET_NAME)
                for s in server.sessions.filter(session_name=_state["session_name"]):
                    s.kill()
            except Exception:
                pass
            if os.path.isdir(session_dir):
                shutil.rmtree(session_dir, ignore_errors=True)

    _got_signal = [False]

    def _signal_handler(signum, frame):
        if _got_signal[0]:
            # Second signal — force exit immediately
            progress("\nForce quit.")
            _cleanup()
            os._exit(130)
        _got_signal[0] = True
        cancel_executor()
        progress("\nCancelling...")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        res = _run_inner(task, toolset_spec, output_format, max_tokens, session_dir, _cleanup, _state, session_ctx=session_ctx)
        if _got_signal[0]:
            _cleanup()
            sys.exit(130)
        return res
    except SystemExit:
        _cleanup()
        raise
    except Exception:
        _cleanup()
        raise


def _setup_session(toolset_spec, session_dir, _state):
    """Set up tmux session and return reusable session context."""
    resolved = resolve_toolset(toolset_spec)

    step(f"Setting up session ({MODEL} · {PROVIDER_NAME})")

    session, panes, actual_session_name = setup_session(resolved["panes"], session_dir=session_dir)
    _state["session_name"] = actual_session_name

    tool_status = check_health(panes)
    available_cmds, missing_cmds = check_commands(resolved["commands"])
    unconfigured = get_unconfigured(resolved["panes"], available_cmds)

    # Compact health line: ✓ pane [type] · ✓ pane [type] · Categories: ...
    health_parts = []
    for name, ok in tool_status.items():
        icon = "✓" if ok else "✗"
        pane_type = ""
        for p in resolved["panes"]:
            if p["name"] == name:
                pane_type = f" [{p['app_type']}]"
                break
        health_parts.append(f"{icon} {name}{pane_type}")
    cats = ", ".join(resolved["categories"]) if resolved.get("categories") else ""
    health_line = " · ".join(health_parts)
    if cats:
        health_line += f" · Categories: {cats}"
    detail(health_line)
    detail(f"Session: {session_dir}")

    tools_summary = build_tools_summary(
        tool_status, available_cmds, resolved["endpoints"],
    )

    return {
        "session": session,
        "session_dir": session_dir,
        "panes": panes,
        "tool_status": tool_status,
        "tools_summary": tools_summary,
        "actual_session_name": actual_session_name,
        "available_cmds": available_cmds,
        "missing_cmds": missing_cmds,
        "endpoints": resolved.get("endpoints", []),
        "unconfigured": unconfigured,
        "categories": set(resolved.get("categories", ["core"])),
    }


def _expand_toolset(category: str, session_ctx: dict) -> bool:
    """Dynamically add a category to the running session. Returns True if expanded."""
    if category in session_ctx.get("categories", set()):
        return False  # already loaded

    cat_def = CATEGORIES.get(category)
    if not cat_def:
        return False

    session = session_ctx.get("session")
    session_dir = session_ctx.get("session_dir")

    # Add new panes
    for pane_id in cat_def.get("panes", []):
        pane_def = PANES.get(pane_id)
        if pane_def and pane_def["name"] not in session_ctx["panes"]:
            pane_info = add_pane(session, pane_def, session_dir)
            session_ctx["panes"][pane_def["name"]] = pane_info
            session_ctx["tool_status"][pane_def["name"]] = {
                "status": "ready",
                "app_type": pane_def["app_type"],
                "description": pane_def["description"],
            }

    # Add new commands
    new_cmds = []
    for cmd_id in cat_def.get("commands", []):
        cmd_def = COMMANDS.get(cmd_id)
        if cmd_def and not any(c["name"] == cmd_id for c in session_ctx["available_cmds"]):
            if not any(c["name"] == cmd_id for c in session_ctx["missing_cmds"]):
                new_cmds.append({"name": cmd_id, **cmd_def})
    if new_cmds:
        avail, miss = check_commands(new_cmds)
        session_ctx["available_cmds"].extend(avail)
        session_ctx["missing_cmds"].extend(miss)

    # Add new endpoints
    for ep_id in cat_def.get("endpoints", []):
        ep_def = ENDPOINTS.get(ep_id)
        if ep_def and not any(e["name"] == ep_id for e in session_ctx["endpoints"]):
            session_ctx["endpoints"].append({"name": ep_id, **ep_def})

    # Check config for new panes
    new_pane_defs = [PANES[pid] for pid in cat_def.get("panes", []) if pid in PANES]
    unconfigured_new = get_unconfigured(new_pane_defs, [])
    session_ctx["unconfigured"].extend(unconfigured_new)

    # Update categories and tools summary
    session_ctx["categories"].add(category)
    session_ctx["tools_summary"] = build_tools_summary(
        session_ctx["tool_status"], session_ctx["available_cmds"], session_ctx["endpoints"],
    )

    detail(f"Added {category}: {', '.join(cat_def.get('panes', []) + cat_def.get('commands', []))}")
    return True


def _progress_event(event_type, *args):
    """Print subtask progress as it happens. Shared callback for run/REPL paths."""
    if event_type == "subtask_start":
        sid, _pane, description = args
        activity(f"[{sid}] {description[:60]}")
    elif event_type == "subtask_done":
        sid, summary, elapsed = args
        detail(f"✓ [{sid}] {summary[:70]} ({elapsed:.1f}s)")
    elif event_type == "subtask_fail":
        sid, msg = args
        detail(f"✗ [{sid}] {msg[:70]}")


def _finalize_summary(summary, output_format, results, elapsed):
    """Wrap summary in JSON envelope if requested; otherwise return as-is."""
    if output_format != "json":
        return summary
    completed = sum(1 for r in results if r.status == SubtaskStatus.COMPLETED)
    total = len(results)
    total_pt = sum(r.prompt_tokens for r in results)
    total_ct = sum(r.completion_tokens for r in results)
    try:
        parsed = json.loads(summary)
    except (ValueError, TypeError):
        parsed = summary
    return json.dumps({
        "result": parsed,
        "success": completed == total,
        "subtasks": {"completed": completed, "total": total},
        "tokens": {"prompt": total_pt, "completion": total_ct, "total": total_pt + total_ct},
        "elapsed_s": round(elapsed, 1),
    }, indent=2)


def _display_final_result(summary, results, elapsed):
    """Print the status banner and indented summary to the user."""
    total_pt = sum(r.prompt_tokens for r in results)
    total_ct = sum(r.completion_tokens for r in results)
    completed = sum(1 for r in results if r.status == SubtaskStatus.COMPLETED)
    total = len(results)
    status_icon = "✓" if completed == total else "✗"
    step(f"Result ({completed}/{total} {status_icon}, {elapsed:.1f}s, {total_pt + total_ct:,} tokens)")
    indented = "\n".join(f"  {line}" for line in summary.splitlines())
    result(indented)


def _build_final_summary(task, results, output_format, session_dir):
    """Pick the summary source: output files for trivial plans, else LLM synthesis."""
    if len(results) == 1 and results[0].status == SubtaskStatus.COMPLETED:
        # Prefer user-created output files over raw terminal output
        return summarizer.read_output_files(session_dir, results[0]) or results[0].summary
    step("Summarizing")
    return summarizer.summarize(task, results, output_format=output_format, session_dir=session_dir)


def _run_inner(task, toolset_spec, output_format, max_tokens, session_dir, _cleanup, _state, session_ctx=None):
    # Set up session if not provided (single-run mode)
    owns_session = session_ctx is None
    if owns_session:
        session_ctx = _setup_session(toolset_spec, session_dir, _state)

    panes = session_ctx["panes"]
    tool_status = session_ctx["tool_status"]
    tools_summary = session_ctx["tools_summary"]

    step(f"Task: {task}")
    start_time = time.time()

    # Per-call context for Tier 1/2: what files are in the session dir and
    # which tasks the user has already run in this REPL/TUI session. Both are
    # computed fresh here so they reflect the state the user just observed.
    session_ctx["session_dir"] = session_dir
    session_ctx["session_files_rendered"] = _render_session_files(session_dir)
    session_ctx["history_rendered"] = _render_recent_history(session_ctx.get("history"))

    # ─── Three-Tier Intent Resolution ──────────────────────────────────
    plan, early_return = route_task(
        task, session_ctx, max_tokens,
        is_direct_fn=_is_direct,
        classify_fn=_classify,
        expand_toolset_fn=_expand_toolset,
        find_cached_fn=_find_cached_plan,
    )
    if early_return is not None:
        return early_return

    # Execution
    step("Executing")
    results = execute_plan(plan, panes, tool_status, on_event=_progress_event,
                           session_dir=session_dir, max_tokens=max_tokens)

    if is_cancelled():
        step("Cancelled")
        return "Cancelled"

    # Recovery replan if some subtasks failed + others were skipped
    results = summarizer.attempt_recovery(
        task, results, execute_plan,
        panes=panes, tool_status=tool_status, tools_summary=tools_summary,
        on_event=_progress_event, session_dir=session_dir, max_tokens=max_tokens,
    )

    # Summarize, format, display
    summary = _build_final_summary(task, results, output_format, session_dir)
    elapsed = time.time() - start_time
    summary = _finalize_summary(summary, output_format, results, elapsed)
    _display_final_result(summary, results, elapsed)

    # Cross-run session log — record task, plan shape, tokens, time
    total_pt = sum(r.prompt_tokens for r in results)
    total_ct = sum(r.completion_tokens for r in results)
    summarizer.log_session(task, plan, results, elapsed, total_pt + total_ct)

    # Within-REPL history: record this task so the next classifier/planner
    # call can resolve follow-up references like "translate the transcript".
    history = session_ctx.get("history")
    if history is not None:
        try:
            produced_files = []
            seen = set()
            for r in results:
                for f in (r.output_files or []):
                    name = os.path.basename(f.get("path", ""))
                    if not name or name.startswith("_") or name in seen:
                        continue
                    seen.add(name)
                    produced_files.append(name)
            short_summary = (summary or "").strip().splitlines()[0][:200] if summary else ""
            history.append({
                "task": task,
                "summary": short_summary,
                "files": produced_files[:6],
            })
        except Exception:
            pass

    # Cleanup session directory (unless --keep-session or REPL mode)
    if owns_session and os.path.isdir(session_dir) and not os.environ.get("CLIVE_KEEP_SESSION"):
        shutil.rmtree(session_dir, ignore_errors=True)

    return summary
