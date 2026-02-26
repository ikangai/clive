"""DAG scheduler and per-subtask worker execution."""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future

from models import Plan, Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from completion import wait_for_ready, wrap_command
from llm import get_client, chat
from prompts import build_worker_prompt
from session import capture_pane, get_meta

# Per-pane locks: only one subtask can use a pane at a time
_pane_locks: dict[str, threading.Lock] = {}


# ─── Command Parsing ──────────────────────────────────────────────────────────

def parse_command(text: str) -> dict:
    """Extract a single XML command from LLM response text."""
    # write_file — pane before path
    m = re.search(
        r'<cmd\s+type=["\']write_file["\'][^>]*pane=["\']([^"\']+)["\'][^>]*path=["\']([^"\']+)["\']>([\s\S]*?)</cmd>',
        text,
    )
    if m:
        return {"type": "write_file", "pane": m.group(1), "path": m.group(2), "value": m.group(3).strip()}

    # write_file — path before pane
    m = re.search(
        r'<cmd\s+type=["\']write_file["\'][^>]*path=["\']([^"\']+)["\'][^>]*pane=["\']([^"\']+)["\']>([\s\S]*?)</cmd>',
        text,
    )
    if m:
        return {"type": "write_file", "pane": m.group(2), "path": m.group(1), "value": m.group(3).strip()}

    # task_complete — no pane needed
    m = re.search(r'<cmd\s+type=["\']task_complete["\']>([\s\S]*?)</cmd>', text)
    if m:
        return {"type": "task_complete", "pane": None, "value": m.group(1).strip()}

    # everything else with pane
    m = re.search(
        r'<cmd\s+type=["\'](\w+)["\'][^>]*pane=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</cmd>',
        text,
    )
    if m:
        return {"type": m.group(1), "pane": m.group(2), "value": m.group(3).strip()}

    # fallback: no pane attribute
    m = re.search(r'<cmd\s+type=["\'](\w+)["\']>([\s\S]*?)</cmd>', text)
    if m:
        return {"type": m.group(1), "pane": None, "value": m.group(2).strip()}

    return {"type": "none", "pane": None, "value": ""}


# ─── File Channel ─────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
        return f"[File: {path} — {len(content.splitlines())} lines]\n{content}"
    except FileNotFoundError:
        return f"[Error: file not found: {path}]"
    except Exception as e:
        return f"[Error reading {path}: {e}]"


def write_file(path: str, content: str) -> str:
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"[Written: {path}]"
    except Exception as e:
        return f"[Error writing {path}: {e}]"


# ─── DAG Scheduler ────────────────────────────────────────────────────────────

def execute_plan(
    plan: Plan,
    panes: dict[str, PaneInfo],
    tool_status: dict[str, dict],
) -> list[SubtaskResult]:
    """Execute all subtasks, respecting DAG dependencies and pane exclusivity."""
    # Initialize per-pane locks (clear stale locks from prior runs)
    _pane_locks.clear()
    for pane_name in panes:
        _pane_locks[pane_name] = threading.Lock()

    results: dict[str, SubtaskResult] = {}
    futures: dict[str, Future] = {}
    subtask_map = {s.id: s for s in plan.subtasks}

    panes_used = {s.pane for s in plan.subtasks}
    max_workers = max(len(panes_used), 1)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while True:
            # Find subtasks ready to run
            for subtask in plan.subtasks:
                if subtask.id in futures or subtask.id in results:
                    continue

                # Check if any dependency failed → skip
                deps_failed = any(
                    dep_id in results
                    and results[dep_id].status in (SubtaskStatus.FAILED, SubtaskStatus.SKIPPED)
                    for dep_id in subtask.depends_on
                )
                if deps_failed:
                    results[subtask.id] = SubtaskResult(
                        subtask_id=subtask.id,
                        status=SubtaskStatus.SKIPPED,
                        summary="Skipped: dependency failed",
                        output_snippet="",
                    )
                    subtask.status = SubtaskStatus.SKIPPED
                    print(f"  SKIP [{subtask.id}] {subtask.description[:50]}... (dependency failed)")
                    continue

                # Check all dependencies completed
                deps_met = all(
                    dep_id in results and results[dep_id].status == SubtaskStatus.COMPLETED
                    for dep_id in subtask.depends_on
                )
                if not deps_met:
                    continue

                # Build context from completed dependencies
                dep_context = _build_dependency_context(subtask, results)

                # Submit to thread pool
                subtask.status = SubtaskStatus.RUNNING
                print(f"  START [{subtask.id}] [{subtask.pane}] {subtask.description[:60]}...")
                future = pool.submit(
                    run_subtask,
                    subtask=subtask,
                    pane_info=panes[subtask.pane],
                    dep_context=dep_context,
                )
                futures[subtask.id] = future

            # Collect completed futures
            collected_any = False
            for sid in list(futures.keys()):
                future = futures[sid]
                if future.done():
                    try:
                        result = future.result()
                    except Exception as e:
                        result = SubtaskResult(
                            subtask_id=sid,
                            status=SubtaskStatus.FAILED,
                            summary=f"Worker crashed: {e}",
                            output_snippet="",
                            error=str(e),
                        )
                    results[sid] = result
                    subtask_map[sid].status = result.status
                    status_str = "DONE" if result.status == SubtaskStatus.COMPLETED else "FAIL"
                    print(f"  {status_str} [{sid}] {result.summary[:60]}")
                    del futures[sid]
                    collected_any = True

            # All subtasks resolved?
            if len(results) == len(plan.subtasks):
                break

            # Re-check for newly unblocked subtasks before deadlock detection
            if collected_any:
                continue

            # Deadlock detection (only when nothing completed this iteration)
            if not futures:
                unresolved = [s.id for s in plan.subtasks if s.id not in results]
                if unresolved:
                    print(f"  WARNING: Deadlocked — no running subtasks, {unresolved} unresolved")
                    for sid in unresolved:
                        results[sid] = SubtaskResult(
                            subtask_id=sid,
                            status=SubtaskStatus.FAILED,
                            summary="Deadlocked: could not start",
                            output_snippet="",
                        )
                break

            time.sleep(0.5)

    return [results[s.id] for s in plan.subtasks]


# ─── Per-Subtask Worker ───────────────────────────────────────────────────────

def run_subtask(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
) -> SubtaskResult:
    """Execute a single subtask. Acquires pane lock, runs mini-loop."""
    client = get_client()
    total_pt = 0
    total_ct = 0

    system_prompt = build_worker_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Begin. Achieve this goal: {subtask.description}"},
    ]

    with _pane_locks[subtask.pane]:
        for turn in range(1, subtask.max_turns + 1):
            # Capture current pane state
            screen = capture_pane(pane_info)
            meta = get_meta(pane_info.pane)
            context = (
                f"[Subtask {subtask.id} Turn {turn}]\n"
                f"[Pane: {subtask.pane}] [Meta: {meta}]\n{screen}"
            )
            messages.append({"role": "user", "content": context})

            # Call LLM
            reply, pt, ct = chat(client, messages)
            total_pt += pt
            total_ct += ct
            messages.append({"role": "assistant", "content": reply})

            print(f"    [{subtask.id}] Turn {turn}: {reply[:80]}...")

            # Parse command
            cmd = parse_command(reply)

            if cmd["type"] == "task_complete":
                return SubtaskResult(
                    subtask_id=subtask.id,
                    status=SubtaskStatus.COMPLETED,
                    summary=cmd["value"],
                    output_snippet=screen[-500:] if len(screen) > 500 else screen,
                    turns_used=turn,
                    prompt_tokens=total_pt,
                    completion_tokens=total_ct,
                )

            elif cmd["type"] == "shell":
                # Wrap shell commands with end marker for reliable detection
                if pane_info.app_type == "shell":
                    wrapped, marker = wrap_command(cmd["value"], subtask.id)
                    pane_info.pane.send_keys(wrapped, enter=True)
                    screen, method = wait_for_ready(pane_info, marker=marker)
                else:
                    pane_info.pane.send_keys(cmd["value"], enter=True)
                    screen, method = wait_for_ready(pane_info)

            elif cmd["type"] == "read_file":
                content = read_file(cmd["value"])
                messages.append({"role": "user", "content": content})
                continue

            elif cmd["type"] == "write_file":
                result = write_file(cmd["path"], cmd["value"])
                messages.append({"role": "user", "content": result})
                continue

            elif cmd["type"] == "none":
                pass  # LLM produced no command, will see pane state next turn

    # Exhausted turns
    final_screen = capture_pane(pane_info)
    return SubtaskResult(
        subtask_id=subtask.id,
        status=SubtaskStatus.FAILED,
        summary=f"Exhausted {subtask.max_turns} turns without completing",
        output_snippet=final_screen[-500:],
        turns_used=subtask.max_turns,
        prompt_tokens=total_pt,
        completion_tokens=total_ct,
    )


def _build_dependency_context(
    subtask: Subtask,
    results: dict[str, SubtaskResult],
) -> str:
    """Build context string from completed dependency results."""
    if not subtask.depends_on:
        return ""

    parts = []
    for dep_id in subtask.depends_on:
        if dep_id in results:
            r = results[dep_id]
            parts.append(f"[Subtask {dep_id} result]: {r.summary}")
            if r.output_snippet:
                parts.append(f"[Subtask {dep_id} last output]:\n{r.output_snippet[:300]}")
    return "\n".join(parts)
