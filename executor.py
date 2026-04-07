"""DAG scheduler and per-subtask worker execution."""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future

log = logging.getLogger(__name__)

from output import progress
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


# ─── Script Extraction ────────────────────────────────────────────────────────

def _extract_script(text: str) -> str:
    """Extract bash script from LLM response."""
    # Try fenced code block (greedy to handle nested blocks)
    m = re.search(r'```(?:bash|sh)?\s*\n([\s\S]*?)```', text)
    if m:
        return m.group(1).strip()
    # Try unfenced: everything from #!/bin/bash to end (or next ```)
    m = re.search(r'(#!/bin/bash[\s\S]*?)(?:```|$)', text)
    if m:
        return m.group(1).strip()
    raise ValueError(f"No script found in response:\n{text[:200]}")


# ─── Script Mode Worker ─────────────────────────────────────────────────────

def run_subtask_script(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute a subtask in script mode: generate → execute → verify → repair loop."""
    from prompts import build_script_prompt
    client = get_client()
    total_pt = 0
    total_ct = 0

    system_prompt = build_script_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Generate the script. Goal: {subtask.description}"},
    ]

    script_path = os.path.join(session_dir, f"_script_{subtask.id}.sh")

    with _pane_locks[subtask.pane]:
        for attempt in range(1, subtask.max_turns + 1):
            reply, pt, ct = chat(client, messages)
            total_pt += pt
            total_ct += ct
            messages.append({"role": "assistant", "content": reply})

            _emit(on_event, "turn", subtask.id, attempt, f"script gen attempt {attempt}")
            _emit(on_event, "tokens", subtask.id, pt, ct)

            try:
                script = _extract_script(reply)
            except ValueError as e:
                progress(f"    [{subtask.id}] Script extraction failed: {e}")
                messages.append({"role": "user", "content": "Error: could not extract script. Respond with a bash script inside ```bash ``` markers."})
                continue

            # Write and execute script (also log to audit trail if available)
            write_file(script_path, script)
            os.chmod(script_path, 0o755)
            try:
                from selfmod.audit import log_attempt
                log_attempt(
                    proposal_id=f"script_{subtask.id}_{attempt}",
                    action="script_generate",
                    files=[script_path],
                    tier="OPEN",
                    roles={},
                    gate_result={"allowed": True, "reason": "script generation"},
                    outcome="generated",
                    details=f"Script for subtask {subtask.id}, attempt {attempt}",
                )
            except Exception:
                pass  # audit logging is best-effort

            wrapped, marker = wrap_command(f"bash {script_path}", subtask.id)
            pane_info.pane.send_keys(wrapped, enter=True)
            screen, method = wait_for_ready(pane_info, marker=marker, max_wait=60.0)

            progress(f"    [{subtask.id}] Script attempt {attempt}: {screen[-80:]}")

            # Check exit code
            exit_check, exit_marker = wrap_command("echo EXIT:$?", subtask.id)
            pane_info.pane.send_keys(exit_check, enter=True)
            exit_screen, _ = wait_for_ready(pane_info, marker=exit_marker)

            exit_code = None
            for line in exit_screen.splitlines():
                if line.strip().startswith("EXIT:"):
                    try:
                        exit_code = int(line.strip().split(":")[1])
                    except (ValueError, IndexError):
                        pass

            if exit_code == 0:
                # Write structured result
                result_path = os.path.join(session_dir, f"_result_{subtask.id}.json")
                output_lines = [l for l in screen.splitlines() if l.strip() and marker not in l]
                summary = output_lines[-1] if output_lines else "Script completed successfully"
                write_file(result_path, json.dumps({
                    "status": "success",
                    "subtask_id": subtask.id,
                    "summary": summary,
                    "turns_used": attempt,
                }, indent=2))

                # Write execution log
                log_path = os.path.join(session_dir, f"_log_{subtask.id}.txt")
                write_file(log_path, screen)

                return SubtaskResult(
                    subtask_id=subtask.id,
                    status=SubtaskStatus.COMPLETED,
                    summary=summary,
                    output_snippet=screen[-500:] if len(screen) > 500 else screen,
                    turns_used=attempt,
                    prompt_tokens=total_pt,
                    completion_tokens=total_ct,
                )

            # Script failed — repair
            progress(f"    [{subtask.id}] Script failed (exit {exit_code}), repairing...")
            messages.append({
                "role": "user",
                "content": f"Script failed with exit code {exit_code}. Terminal output:\n\n{screen[-1000:]}\n\nFix the script and provide the corrected version.",
            })

    final_screen = capture_pane(pane_info)
    # Write failure log
    log_path = os.path.join(session_dir, f"_log_{subtask.id}.txt")
    write_file(log_path, final_screen)

    return SubtaskResult(
        subtask_id=subtask.id,
        status=SubtaskStatus.FAILED,
        summary=f"Script mode exhausted {subtask.max_turns} attempts",
        output_snippet=final_screen[-500:],
        turns_used=subtask.max_turns,
        prompt_tokens=total_pt,
        completion_tokens=total_ct,
    )


# ─── DAG Scheduler ────────────────────────────────────────────────────────────

def _emit(on_event, *args):
    """Call event callback if provided."""
    if on_event:
        try:
            on_event(*args)
        except Exception:
            log.debug("on_event callback failed for %s", args[0] if args else "?", exc_info=True)


def execute_plan(
    plan: Plan,
    panes: dict[str, PaneInfo],
    tool_status: dict[str, dict],
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> list[SubtaskResult]:
    """Execute all subtasks, respecting DAG dependencies and pane exclusivity.

    on_event is an optional callback for live status updates:
        ("subtask_start", subtask_id, pane, description)
        ("subtask_done",  subtask_id, summary, elapsed)
        ("subtask_fail",  subtask_id, error)
        ("subtask_skip",  subtask_id, reason)
        ("turn",          subtask_id, turn_num, command_snippet)
        ("tokens",        subtask_id, prompt_tokens, completion_tokens)
    """
    # Initialize per-pane locks (clear stale locks from prior runs)
    _pane_locks.clear()
    for pane_name in panes:
        _pane_locks[pane_name] = threading.Lock()

    results: dict[str, SubtaskResult] = {}
    futures: dict[str, Future] = {}
    subtask_map = {s.id: s for s in plan.subtasks}
    start_times: dict[str, float] = {}

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
                    progress(f"  SKIP [{subtask.id}] {subtask.description[:50]}... (dependency failed)")
                    _emit(on_event, "subtask_skip", subtask.id, "dependency failed")
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
                start_times[subtask.id] = time.time()
                progress(f"  START [{subtask.id}] [{subtask.pane}] {subtask.description[:60]}...")
                _emit(on_event, "subtask_start", subtask.id, subtask.pane, subtask.description)
                future = pool.submit(
                    run_subtask,
                    subtask=subtask,
                    pane_info=panes[subtask.pane],
                    dep_context=dep_context,
                    on_event=on_event,
                    session_dir=session_dir,
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
                    # Script→interactive fallback: retry failed script subtasks as interactive
                    subtask_obj = subtask_map[sid]
                    if (result.status == SubtaskStatus.FAILED
                            and subtask_obj.mode == "script"
                            and not getattr(subtask_obj, '_retried', False)):
                        progress(f"  RETRY [{sid}] script failed, retrying as interactive")
                        _emit(on_event, "subtask_fail", sid, f"script failed, retrying interactive")
                        subtask_obj.mode = "interactive"
                        subtask_obj._retried = True
                        subtask_obj.status = SubtaskStatus.PENDING
                        del futures[sid]
                        collected_any = True
                        continue

                    results[sid] = result
                    subtask_map[sid].status = result.status
                    elapsed = time.time() - start_times.get(sid, time.time())
                    status_str = "DONE" if result.status == SubtaskStatus.COMPLETED else "FAIL"
                    progress(f"  {status_str} [{sid}] {result.summary[:60]}")
                    if result.status == SubtaskStatus.COMPLETED:
                        _emit(on_event, "subtask_done", sid, result.summary, elapsed)
                    else:
                        _emit(on_event, "subtask_fail", sid, result.summary)
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
                    progress(f"  WARNING: Deadlocked — no running subtasks, {unresolved} unresolved")
                    for sid in unresolved:
                        results[sid] = SubtaskResult(
                            subtask_id=sid,
                            status=SubtaskStatus.FAILED,
                            summary="Deadlocked: could not start",
                            output_snippet="",
                        )
                        _emit(on_event, "subtask_fail", sid, "Deadlocked: could not start")
                break

            time.sleep(0.5)

    return [results[s.id] for s in plan.subtasks]


# ─── Per-Subtask Worker ───────────────────────────────────────────────────────

def run_subtask(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute a single subtask. Dispatches based on observation level (mode)."""
    if subtask.mode == "script":
        return run_subtask_script(
            subtask=subtask,
            pane_info=pane_info,
            dep_context=dep_context,
            on_event=on_event,
            session_dir=session_dir,
        )

    # Streaming mode uses interactive loop with intervention detection
    detect_intervention = subtask.mode == "streaming"

    # Interactive/streaming mode: turn-by-turn observation loop
    client = get_client()
    total_pt = 0
    total_ct = 0

    system_prompt = build_worker_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Begin. Achieve this goal: {subtask.description}"},
    ]

    with _pane_locks[subtask.pane]:
        for turn in range(1, subtask.max_turns + 1):
            # Capture current pane state
            screen = capture_pane(pane_info)

            # Check for DONE: protocol (clive-to-clive communication)
            for line in screen.splitlines():
                if line.strip().startswith("DONE:"):
                    done_payload = line.strip()[5:].strip()
                    try:
                        done_data = json.loads(done_payload)
                        summary = done_data.get("result", done_data.get("reason", str(done_data)))
                        status = SubtaskStatus.COMPLETED if done_data.get("status") == "success" else SubtaskStatus.FAILED
                    except (json.JSONDecodeError, AttributeError):
                        summary = done_payload
                        status = SubtaskStatus.COMPLETED
                    return SubtaskResult(
                        subtask_id=subtask.id, status=status, summary=summary,
                        output_snippet=screen[-500:], turns_used=turn,
                        prompt_tokens=total_pt, completion_tokens=total_ct,
                    )

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

            progress(f"    [{subtask.id}] Turn {turn}: {reply[:80]}...")

            # Parse command
            cmd = parse_command(reply)

            # Emit turn event with command snippet
            cmd_snippet = cmd["value"][:80] if cmd["value"] else cmd["type"]
            _emit(on_event, "turn", subtask.id, turn, cmd_snippet)
            _emit(on_event, "tokens", subtask.id, pt, ct)

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
                    screen, method = wait_for_ready(
                        pane_info, marker=marker,
                        detect_intervention=detect_intervention,
                    )
                else:
                    pane_info.pane.send_keys(cmd["value"], enter=True)
                    screen, method = wait_for_ready(
                        pane_info,
                        detect_intervention=detect_intervention,
                    )

                # If intervention detected, inject context for agent to handle
                if method.startswith("intervention:"):
                    intervention_type = method.split(":", 1)[1]
                    messages.append({
                        "role": "user",
                        "content": f"[INTERVENTION DETECTED: {intervention_type}] "
                                   f"The command needs your input. Screen:\n{screen}",
                    })
                    continue  # extra turn to handle the intervention

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
