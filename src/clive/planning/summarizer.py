"""Result synthesis, session logging, and failure recovery.

Extracted from clive.py to isolate the post-execution phase:
    - attempt_recovery(): rerun failed subtasks via a fresh planner call
    - read_output_files(): prefer user-created files over raw terminal output
    - summarize(): final LLM call to synthesize all subtask results
    - log_session(): append structured record to the cross-run session log
"""

import json
import os
import time

from file_inspect import sniff_session_files
from llm import chat, get_client
from models import SubtaskStatus
from output import detail, step
from planner import create_plan
from prompts import build_summarizer_prompt


SESSION_LOG = os.path.expanduser("~/.clive_session_log.jsonl")


def attempt_recovery(task, results, plan_execute_fn, panes, tool_status,
                     tools_summary, on_event, session_dir, max_tokens):
    """When some subtasks failed and others were skipped, replan and retry.

    Only triggers when failure count is small (<=2) — larger failures suggest
    fundamental planning problems that replanning won't fix. Extends and
    returns the combined result list; on exception, returns original results.
    """
    failed = [r for r in results if r.status == SubtaskStatus.FAILED]
    skipped = [r for r in results if r.status == SubtaskStatus.SKIPPED]
    if not (failed and skipped and len(failed) <= 2):
        return results

    step("Replanning")
    detail("Some subtasks failed, attempting recovery...")
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
            detail("Replanned — executing recovery subtasks...")
            tokens_used = sum(r.prompt_tokens + r.completion_tokens for r in results)
            replan_budget = max(max_tokens - tokens_used, 5000)
            recovery_results = plan_execute_fn(
                replan, panes, tool_status,
                on_event=on_event, session_dir=session_dir, max_tokens=replan_budget,
            )
            results.extend(recovery_results)
    except Exception as e:
        detail(f"Replan failed: {e}")
    return results


def read_output_files(session_dir, result):
    """Read user-created output files tracked by the subtask result.

    Internal files (prefixed with `_`) are skipped so we surface the user's
    actual artifacts, not Clive's scratch files.
    """
    if not session_dir:
        return ""
    content_parts = []
    for f in result.output_files or []:
        path = f.get("path", "")
        if not path or not os.path.isfile(path):
            continue
        if os.path.basename(path).startswith("_"):
            continue
        try:
            with open(path, "r", errors="replace") as fh:
                text = fh.read(4000)
            if text.strip():
                content_parts.append(text.strip())
        except OSError:
            continue
    return "\n".join(content_parts) if content_parts else ""


def summarize(task, results, output_format="default", session_dir=""):
    """Final LLM call to synthesize all subtask results into a user-facing answer."""
    client = get_client()

    result_text = "\n\n".join(
        f"Subtask {r.subtask_id} [{r.status.value}]: {r.summary}"
        for r in results
    )

    # Read key output files for richer summarization
    file_context = ""
    if session_dir:
        all_files = []
        for r in results:
            all_files.extend(sniff_session_files(session_dir, r.subtask_id))
        # Include preview of top files (up to 500 chars total)
        previews = []
        total_chars = 0
        for f in all_files:
            if f.get("preview") and total_chars < 500:
                previews.append(f"  {f['path']}: {f['preview'][:200]}")
                total_chars += len(previews[-1])
        if previews:
            file_context = "\n\nKey output files:\n" + "\n".join(previews)

    messages = [
        {"role": "system", "content": build_summarizer_prompt(output_format)},
        {"role": "user", "content": f"Original task: {task}\n\nSubtask results:\n{result_text}{file_context}"},
    ]

    content, _, _ = chat(client, messages)
    return content


def log_session(task, plan, results, elapsed, total_tokens):
    """Append a session record for cross-run learning and plan caching."""
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "task": task[:200],
        "subtasks": len(plan.subtasks),
        "modes": [s.mode for s in plan.subtasks],
        "steps": [{"desc": s.description[:100], "pane": s.pane, "mode": s.mode} for s in plan.subtasks],
        "completed": sum(1 for r in results if r.status == SubtaskStatus.COMPLETED),
        "failed": sum(1 for r in results if r.status == SubtaskStatus.FAILED),
        "tokens": total_tokens,
        "elapsed_s": round(elapsed, 1),
    }
    try:
        with open(SESSION_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
