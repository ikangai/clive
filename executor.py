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

# ─── Command Safety ──────────────────────────────────────────────────────────

BLOCKED_COMMANDS = [
    re.compile(r'rm\s+(-\w*)*\s*-rf\s+/\s*$'),
    re.compile(r'\b(shutdown|reboot|halt|poweroff)\b'),
    re.compile(r'\bmkfs\b'),
    re.compile(r'\bdd\s+.*of=/dev/'),
    re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'),
    re.compile(r'>\s*/dev/sd[a-z]'),
]


def _check_command_safety(command: str) -> str | None:
    """Check command against blocklist. Returns violation or None."""
    for pattern in BLOCKED_COMMANDS:
        if pattern.search(command):
            return f"Blocked dangerous command: {command[:80]}"
    return None


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
    log.info(f"Subtask {subtask.id}: script mode, pane={subtask.pane}")
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

            # Execute script and capture exit code in one round-trip
            import uuid as _uuid
            nonce = _uuid.uuid4().hex[:4]
            marker = f"___DONE_{subtask.id}_{nonce}___"
            combined = f'bash {script_path}; echo "EXIT:$? {marker}"'
            pane_info.pane.send_keys(combined, enter=True)
            screen, method = wait_for_ready(pane_info, marker=marker, max_wait=60.0)

            progress(f"    [{subtask.id}] Script attempt {attempt}: {screen[-80:]}")

            # Parse exit code from the combined marker line
            exit_code = None
            for line in screen.splitlines():
                if marker in line and "EXIT:" in line:
                    try:
                        exit_part = line.split("EXIT:")[1].split()[0]
                        exit_code = int(exit_part)
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
    max_tokens: int = 50000,
) -> list[SubtaskResult]:
    """Execute all subtasks, respecting DAG dependencies and pane exclusivity.

    Features:
    - Event-driven scheduling (instant wake on subtask completion)
    - Pane state continuity (pass last screen to next subtask on same pane)
    - Result file registry (track files written by each subtask)
    - Branch cancellation (cancel running subtasks when deps fail)
    - Token budget enforcement

    on_event is an optional callback for live status updates:
        ("subtask_start", subtask_id, pane, description)
        ("subtask_done",  subtask_id, summary, elapsed)
        ("subtask_fail",  subtask_id, error)
        ("subtask_skip",  subtask_id, reason)
        ("turn",          subtask_id, turn_num, command_snippet)
        ("tokens",        subtask_id, prompt_tokens, completion_tokens)
    """
    # Per-plan pane locks (scoped to this execution, not module-level)
    plan_locks: dict[str, threading.Lock] = {}
    for pane_name in panes:
        plan_locks[pane_name] = threading.Lock()
    _pane_locks.update(plan_locks)

    results: dict[str, SubtaskResult] = {}
    futures: dict[str, Future] = {}
    subtask_map = {s.id: s for s in plan.subtasks}
    start_times: dict[str, float] = {}

    # Pane state continuity: track last screen per pane for handoff
    pane_last_screen: dict[str, str] = {}

    # Result file registry: track files written by each subtask
    result_files: dict[str, list[str]] = {}

    panes_used = {s.pane for s in plan.subtasks}
    max_workers = max(len(panes_used), 1)

    # Event-driven scheduling: wake instantly when a future completes
    wake_event = threading.Event()

    def _on_future_done(fut):
        wake_event.set()

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

                # Build enriched dependency context (with file registry)
                dep_context = _build_dependency_context(subtask, results, result_files)

                # Pane state continuity: pass last screen from previous subtask
                pane_context = pane_last_screen.get(subtask.pane, "")

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
                    pane_context=pane_context,
                )
                future.add_done_callback(_on_future_done)
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
                    # Script→interactive fallback
                    subtask_obj = subtask_map[sid]
                    if (result.status == SubtaskStatus.FAILED
                            and subtask_obj.mode == "script"
                            and not subtask_obj._retried):
                        progress(f"  RETRY [{sid}] script failed, retrying as interactive")
                        _emit(on_event, "subtask_fail", sid, f"script failed, retrying interactive")
                        subtask_obj.mode = "interactive"
                        subtask_obj.max_turns = max(subtask_obj.max_turns, 10)
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

                    # Track pane state and result files
                    pane_last_screen[subtask_obj.pane] = result.output_snippet
                    if result.status == SubtaskStatus.COMPLETED:
                        _emit(on_event, "subtask_done", sid, result.summary, elapsed)
                        # Scan session dir for files written by this subtask
                        _track_result_files(sid, session_dir, result_files, result)
                    else:
                        _emit(on_event, "subtask_fail", sid, result.summary)
                        # Branch cancellation: cancel futures whose results are now useless
                        _cancel_orphaned_branches(sid, plan, results, futures, on_event)

                    del futures[sid]
                    collected_any = True

                    # Token budget enforcement
                    total_tokens = sum(r.prompt_tokens + r.completion_tokens for r in results.values())
                    if total_tokens > max_tokens:
                        progress(f"  TOKEN BUDGET EXCEEDED: {total_tokens:,} > {max_tokens:,}")
                        for remaining_sid in list(futures.keys()):
                            futures[remaining_sid].cancel()
                            del futures[remaining_sid]
                        for s in plan.subtasks:
                            if s.id not in results:
                                results[s.id] = SubtaskResult(
                                    subtask_id=s.id,
                                    status=SubtaskStatus.SKIPPED,
                                    summary="Skipped: token budget exceeded",
                                    output_snippet="",
                                )
                        break

            # All subtasks resolved?
            if len(results) == len(plan.subtasks):
                break

            # Re-check for newly unblocked subtasks before deadlock detection
            if collected_any:
                wake_event.clear()
                continue

            # Deadlock detection
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

            # Event-driven wait: wake instantly when a future completes
            wake_event.wait(timeout=0.5)
            wake_event.clear()

    return [results[s.id] for s in plan.subtasks]


def _track_result_files(subtask_id: str, session_dir: str, registry: dict[str, list[str]],
                        result: SubtaskResult | None = None):
    """Scan session dir for files, inspect them for schema info. Update registry."""
    from file_inspect import sniff_session_files
    file_infos = sniff_session_files(session_dir, subtask_id)
    registry[subtask_id] = file_infos
    # Also enrich the SubtaskResult with file metadata
    if result is not None:
        result.output_files = file_infos


def _cancel_orphaned_branches(
    failed_sid: str,
    plan: Plan,
    results: dict[str, SubtaskResult],
    futures: dict[str, Future],
    on_event=None,
):
    """Cancel running subtasks whose results will never be used.

    When a subtask fails, check if any running subtask's dependents
    ALL depend on the failed subtask (making the running work useless).
    """
    # Find all subtasks that depend (directly or transitively) on the failed one
    unreachable = set()
    for s in plan.subtasks:
        if failed_sid in s.depends_on and s.id not in results:
            unreachable.add(s.id)

    # Cancel futures for unreachable subtasks
    for sid in list(futures.keys()):
        if sid in unreachable:
            futures[sid].cancel()
            results[sid] = SubtaskResult(
                subtask_id=sid,
                status=SubtaskStatus.SKIPPED,
                summary=f"Skipped: branch cancelled (dep {failed_sid} failed)",
                output_snippet="",
            )
            progress(f"  CANCEL [{sid}] branch cancelled (dep {failed_sid} failed)")
            _emit(on_event, "subtask_skip", sid, f"branch cancelled")
            del futures[sid]


def _trim_messages(messages: list[dict], max_user_turns: int = 4) -> list[dict]:
    """Trim conversation history to system prompt + first turn + last N turns.

    Bookend strategy: keeps the first user turn (initial screen context —
    working directory, available files) alongside the most recent turns.
    Prevents unbounded growth while preserving critical early context.
    """
    if not messages:
        return messages

    system = [m for m in messages if m["role"] == "system"]
    conversation = [m for m in messages if m["role"] != "system"]

    user_indices = [i for i, m in enumerate(conversation) if m["role"] == "user"]

    if len(user_indices) <= max_user_turns:
        return messages

    # Keep first user-assistant pair (initial context) + last N-1 pairs
    first_pair = conversation[:2] if len(conversation) >= 2 else conversation[:1]
    cutoff_idx = user_indices[-(max_user_turns - 1)] if max_user_turns > 1 else user_indices[-1]
    recent = conversation[cutoff_idx:]

    return system + first_pair + recent


# ─── Per-Subtask Worker ───────────────────────────────────────────────────────

def run_subtask(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
    pane_context: str = "",
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
    log.info(f"Subtask {subtask.id}: mode={subtask.mode}, pane={subtask.pane}, max_turns={subtask.max_turns}")

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

    # Pane state continuity: include previous subtask's screen if available
    begin_msg = f"Begin. Achieve this goal: {subtask.description}"
    if pane_context:
        begin_msg += f"\n\n[Previous task on this pane left the screen showing:]\n{pane_context[-500:]}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": begin_msg},
    ]

    last_screen = None
    no_change_count = 0
    NO_CHANGE_LIMIT = 3  # stop if screen unchanged for this many consecutive turns

    with _pane_locks[subtask.pane]:
        for turn in range(1, subtask.max_turns + 1):
            # Capture current pane state
            screen = capture_pane(pane_info)

            # No-change early stop: if screen is identical for N turns, the task is stuck
            if last_screen is not None and screen == last_screen:
                no_change_count += 1
                if no_change_count >= NO_CHANGE_LIMIT:
                    progress(f"    [{subtask.id}] Screen unchanged for {NO_CHANGE_LIMIT} turns, stopping")
                    return SubtaskResult(
                        subtask_id=subtask.id,
                        status=SubtaskStatus.FAILED,
                        summary=f"Stuck: screen unchanged for {NO_CHANGE_LIMIT} consecutive turns",
                        output_snippet=screen[-500:] if len(screen) > 500 else screen,
                        turns_used=turn,
                        prompt_tokens=total_pt,
                        completion_tokens=total_ct,
                    )
            else:
                no_change_count = 0

            # Check for DONE: protocol (clive-to-clive, agent panes only)
            if pane_info.app_type == "agent":
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

            from screen_diff import compute_screen_diff
            screen_content = compute_screen_diff(last_screen, screen)
            last_screen = screen
            meta = get_meta(pane_info.pane)

            # Read shared scratchpad for cross-agent discoveries
            scratchpad_note = ""
            scratchpad_path = os.path.join(session_dir, "_scratchpad.jsonl")
            if os.path.exists(scratchpad_path):
                try:
                    with open(scratchpad_path, "r") as sf:
                        notes = [l.strip() for l in sf.readlines()[-5:] if l.strip()]
                    if notes:
                        scratchpad_note = f"\n[Scratchpad from other agents]:\n" + "\n".join(notes)
                except OSError:
                    pass

            context = (
                f"[Subtask {subtask.id} Turn {turn}]\n"
                f"[Pane: {subtask.pane}] [Meta: {meta}]\n{screen_content}"
                f"{scratchpad_note}"
            )
            messages.append({"role": "user", "content": context})

            # Trim context to prevent unbounded growth
            messages = _trim_messages(messages, max_user_turns=4)

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
                # Safety check before sending to pane
                violation = _check_command_safety(cmd["value"])
                if violation:
                    log.warning(violation)
                    messages.append({"role": "user", "content": f"[BLOCKED] {violation}. Choose a safer approach."})
                    continue

                # Wrap shell commands with end marker for reliable detection
                # All shell-like panes benefit from markers (avoid 2s idle timeout)
                _SHELL_LIKE = {"shell", "data", "docs", "media", "browser", "files"}
                if pane_info.app_type in _SHELL_LIKE:
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
                no_change_count = 0  # file ops don't change screen but are progress
                continue

            elif cmd["type"] == "write_file":
                result = write_file(cmd["path"], cmd["value"])
                messages.append({"role": "user", "content": result})
                no_change_count = 0  # file ops don't change screen but are progress
                continue

            elif cmd["type"] == "wait":
                # Agent explicitly requests to wait and re-observe
                wait_secs = 2
                try:
                    wait_secs = max(1, min(int(cmd["value"]), 10))
                except (ValueError, TypeError):
                    pass
                progress(f"    [{subtask.id}] Waiting {wait_secs}s...")
                time.sleep(wait_secs)

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
    result_files: dict[str, list[dict]] | None = None,
) -> str:
    """Build semantic dependency context from completed results.

    Compact, scannable format with schema info from file inspection.
    Downstream agents see exactly what data is available.
    """
    if not subtask.depends_on:
        return ""

    parts = ["Dependencies completed:"]
    for dep_id in subtask.depends_on:
        if dep_id not in results:
            continue
        r = results[dep_id]
        status = "DONE" if r.status == SubtaskStatus.COMPLETED else "FAIL"
        parts.append(f"  [{dep_id}] {status}: {r.summary}")

        # Include file info with schema detection
        if result_files and dep_id in result_files:
            from file_inspect import format_file_context
            file_ctx = format_file_context(result_files[dep_id])
            if file_ctx:
                parts.append(file_ctx)

        # For failures, include error detail
        if r.error:
            parts.append(f"  [{dep_id}] Error: {r.error[:200]}")

    return "\n".join(parts)
