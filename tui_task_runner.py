"""TUI task execution — triage → plan → execute → summarize.

Extracted from tui.py. These were methods on CliveApp; they now take
`app` (the CliveApp instance) as an explicit parameter. The caller
(CliveApp._execute_task) delegates to run_task_inner().
"""

import json
import os
import shutil
import time

from textual.widgets import RichLog

from executor import execute_plan
from llm import chat, get_client
from models import SubtaskStatus
from planner import create_plan
from prompts import build_summarizer_prompt, build_triage_prompt
from session import check_health, generate_session_id, setup_session
from toolsets import build_tools_summary

from tui_helpers import build_clive_context


def tui_triage(app, task: str, task_info: dict, out: RichLog) -> dict:
    """Run the triage prompt. Returns a dict with action/task/response/question."""
    client = get_client()
    clive_context = build_clive_context(
        app._spec, app._resolved, app._available_cmds, app._missing_cmds,
    )
    triage_msgs = [
        {"role": "system", "content": build_triage_prompt(clive_context)},
        {"role": "user", "content": task},
    ]
    try:
        triage_raw, pt, ct = chat(client, triage_msgs, max_tokens=512)
        task_info["pt"] += pt
        task_info["ct"] += ct
        clean = triage_raw.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(clean)
    except Exception:
        return {"action": "execute", "task": task}


def tui_show_plan(app, plan, out: RichLog) -> None:
    """Render a plan's subtasks to the RichLog."""
    app.call_from_thread(out.write, "")
    for s in plan.subtasks:
        deps = f" [#3a3a4a]→ {', '.join(s.depends_on)}[/]" if s.depends_on else ""
        app.call_from_thread(
            out.write,
            f"  [#3a3a4a]○[/] [#c9c9d6]{s.id}[/] [#6b7280]{s.pane}[/] {s.description[:55]}{deps}",
        )
    app.call_from_thread(out.write, "")


def tui_summarize_results(app, task: str, results, task_info: dict) -> str:
    """Final LLM call to synthesize results. Returns the summary string."""
    try:
        client = get_client()
        result_text = "\n\n".join(
            f"Subtask {r.subtask_id} [{r.status.value}]: {r.summary}"
            for r in results
        )
        messages = [
            {"role": "system", "content": build_summarizer_prompt()},
            {"role": "user", "content": f"Original task: {task}\n\nSubtask results:\n{result_text}"},
        ]
        summary, pt, ct = chat(client, messages)
        task_info["pt"] += pt
        task_info["ct"] += ct
        return summary
    except Exception as e:
        return f"Summarization failed: {e}"


def tui_render_summary(app, summary: str, results, task_info: dict, out: RichLog) -> None:
    """Print the final summary line + body to the RichLog."""
    completed = sum(1 for r in results if r.status == SubtaskStatus.COMPLETED)
    total = len(results)
    elapsed = time.time() - task_info["start"]
    total_tokens = task_info["pt"] + task_info["ct"]
    app.call_from_thread(out.write, "")
    app.call_from_thread(
        out.write,
        f"[#22c55e]✓ {completed}/{total} subtasks[/] [#3a3a4a]in {elapsed:.1f}s · {total_tokens:,} tokens[/]",
    )
    app.call_from_thread(out.write, "")
    for line in summary.split("\n"):
        app.call_from_thread(out.write, line)
    app.call_from_thread(out.write, "")


def run_task_inner(app, task: str, task_info: dict, out: RichLog) -> None:
    session_id = generate_session_id()
    session_dir = f"/tmp/clive/{session_id}"

    triage = tui_triage(app, task, task_info, out)
    action = triage.get("action", "execute")

    if action == "answer":
        app.call_from_thread(out.write, "")
        for line in triage.get("response", "").split("\n"):
            app.call_from_thread(out.write, line)
        app.call_from_thread(out.write, "")
        return

    if action == "clarify":
        question = triage.get("question", "Could you provide more details?")
        app.call_from_thread(out.write, f"\n[#d97706]?[/] {question}\n")
        app._pending = {"task": task}
        return

    # action == "execute"
    task = triage.get("task", task)
    task_info["desc"] = task

    app.call_from_thread(out.write, "[#6b7280]Setting up session...[/]")
    try:
        session, panes, _session_name = setup_session(app._resolved["panes"], session_dir=session_dir)
        tool_status = check_health(panes)
    except Exception as e:
        app.call_from_thread(out.write, f"[#ef4444]✗ Session failed: {e}[/]")
        return

    tools_summary = build_tools_summary(
        tool_status, app._available_cmds, app._resolved["endpoints"]
    )
    if app._cancelled.is_set():
        return

    app.call_from_thread(out.write, "[#6b7280]Planning...[/]")
    try:
        plan = create_plan(task, panes, tool_status, tools_summary=tools_summary)
    except Exception as e:
        app.call_from_thread(out.write, f"[#ef4444]✗ Planning failed: {e}[/]")
        return

    tui_show_plan(app, plan, out)
    if app._cancelled.is_set():
        return

    try:
        results = execute_plan(
            plan, panes, tool_status,
            on_event=lambda et, *a: app._on_event(et, task_info, *a),
            session_dir=session_dir,
        )
    except Exception as e:
        app.call_from_thread(out.write, f"[#ef4444]✗ Execution failed: {e}[/]")
        return

    if app._cancelled.is_set():
        return

    app.call_from_thread(out.write, "")
    app.call_from_thread(out.write, "[#6b7280]Summarizing...[/]")
    summary = tui_summarize_results(app, task, results, task_info)
    tui_render_summary(app, summary, results, task_info, out)

    # Cleanup session directory
    if session_dir and os.path.isdir(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)
