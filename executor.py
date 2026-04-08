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
from llm import get_client, chat, chat_stream
from prompts import build_worker_prompt
from session import capture_pane, get_meta
from screen_diff import compute_screen_diff

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


# ─── Outcome Detection ───────────────────────────────────────────────────────

_SUCCESS_PATTERNS = [
    re.compile(r'\b(saved|written|created|completed|success|done|ok)\b', re.IGNORECASE),
]
_ERROR_PATTERNS = [
    re.compile(r'\b(error|failed|not found|no such|cannot|denied|refused)\b', re.IGNORECASE),
]


def _detect_outcome_signal(screen: str) -> str:
    """Detect semantic success/failure from screen content (no LLM, just regex)."""
    last_lines = "\n".join(screen.splitlines()[-5:])
    errors = any(p.search(last_lines) for p in _ERROR_PATTERNS)
    successes = any(p.search(last_lines) for p in _SUCCESS_PATTERNS)
    if errors and not successes:
        return "error indicators detected"
    if successes and not errors:
        return "success indicators detected"
    return ""


def _auto_verify_command(command: str, session_dir: str) -> str:
    """Auto-verify file writes after shell commands. Saves verification turns.

    Detects redirect operators (>, >>) and checks if the target file exists.
    Returns a verification string or empty if nothing to verify.
    """
    # Detect file writes: cmd > file or cmd >> file
    m = re.search(r'>\s*(\S+)\s*$', command)
    if not m:
        m = re.search(r'>>\s*(\S+)\s*$', command)
    if not m:
        return ""

    target = m.group(1).strip("'\"")
    # Resolve relative to session_dir if not absolute
    if not target.startswith("/"):
        target = os.path.join(session_dir, target)

    if os.path.exists(target):
        size = os.path.getsize(target)
        # Quick content check for JSON/CSV validity
        if target.endswith(".json") and size > 0:
            try:
                import json as _json
                _json.load(open(target))
                return f"{os.path.basename(target)} exists, {size} bytes, valid JSON"
            except Exception:
                return f"{os.path.basename(target)} exists, {size} bytes, invalid JSON"
        return f"{os.path.basename(target)} exists, {size} bytes"
    return ""


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


def parse_commands(text: str) -> list[dict]:
    """Extract ALL commands from LLM response (for pipelining).

    Returns a list of command dicts. If only one command found,
    returns a single-element list. Falls back to parse_command for
    responses with just one command.
    """
    cmds = []
    # Find all <cmd ...>...</cmd> blocks
    for m in re.finditer(r'<cmd\s+[^>]*>[\s\S]*?</cmd>', text):
        cmd = parse_command(m.group(0))
        if cmd["type"] != "none":
            cmds.append(cmd)
    return cmds if cmds else [parse_command(text)]


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
    """Extract bash or Python script from LLM response."""
    # Try fenced code block (bash, sh, or python)
    m = re.search(r'```(?:bash|sh|python[3]?)?\s*\n([\s\S]*?)```', text)
    if m:
        return m.group(1).strip()
    # Try unfenced: everything from shebang to end (or next ```)
    m = re.search(r'(#!(?:/bin/bash|/usr/bin/env python[3]?)[\s\S]*?)(?:```|$)', text)
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

    # Script path determined by language after extraction
    default_script_path = os.path.join(session_dir, f"_script_{subtask.id}.sh")

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

            # Detect language from shebang and set path/executor accordingly
            if script.startswith("#!/usr/bin/env python") or script.startswith("#!/usr/bin/python"):
                script_path = os.path.join(session_dir, f"_script_{subtask.id}.py")
                script_executor = "python3"
            else:
                script_path = default_script_path
                script_executor = "bash"

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
            combined = f'{script_executor} {script_path}; echo "EXIT:$? {marker}"'
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
    # Plan-to-script compiler: collapse sequential all-script same-pane plans
    plan = _try_collapse_plan(plan)

    # Create shared brain + per-pane agents
    from pane_agent import PaneAgent, SharedBrain
    shared_brain = SharedBrain(session_dir)

    # Try to load persisted agent state from previous runs
    agent_state_dir = os.path.expanduser("~/.clive/agents")

    pane_agents: dict[str, PaneAgent] = {}
    for pane_name, pane_info in panes.items():
        agent = PaneAgent(pane_info, session_dir=session_dir, shared_brain=shared_brain)
        # Load persisted memory/shortcuts from previous sessions
        state_path = os.path.join(agent_state_dir, f"{pane_name}.json")
        agent.load_state(state_path)
        pane_agents[pane_name] = agent

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

                # Build plan context — agent knows its role in the bigger picture
                plan_summary = _build_plan_context(plan, subtask)

                # Current token usage for budget awareness
                tokens_used = sum(r.prompt_tokens + r.completion_tokens for r in results.values())

                # Submit to thread pool
                subtask.status = SubtaskStatus.RUNNING
                start_times[subtask.id] = time.time()
                progress(f"  START [{subtask.id}] [{subtask.pane}] {subtask.description[:60]}...")
                _emit(on_event, "subtask_start", subtask.id, subtask.pane, subtask.description)
                # Use PaneAgent for context continuity across subtasks
                agent = pane_agents.get(subtask.pane)
                if agent:
                    future = pool.submit(
                        agent.execute,
                        subtask=subtask,
                        dep_context=dep_context,
                        on_event=on_event,
                        plan_context=plan_summary,
                        tokens_used=tokens_used,
                        max_tokens=max_tokens,
                    )
                else:
                    future = pool.submit(
                        run_subtask,
                        subtask=subtask,
                        pane_info=panes[subtask.pane],
                        dep_context=dep_context,
                        on_event=on_event,
                        session_dir=session_dir,
                        pane_context=pane_context,
                        plan_context=plan_summary,
                        tokens_used=tokens_used,
                        max_tokens=max_tokens,
                        all_panes=panes,
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

    # Persist agent state for cross-run continuity
    os.makedirs(agent_state_dir, exist_ok=True)
    for pane_name, agent in pane_agents.items():
        agent.save(os.path.join(agent_state_dir, f"{pane_name}.json"))
    shared_brain.save(os.path.join(agent_state_dir, "_shared_brain.json"))

    return [results[s.id] for s in plan.subtasks]


def _write_recovery_pattern(session_dir: str, agent: str, successful_cmd: str):
    """Write an error-recovery pattern to the scratchpad for parallel agents."""
    scratchpad = os.path.join(session_dir, "_scratchpad.jsonl")
    try:
        with open(scratchpad, "a") as f:
            f.write(json.dumps({
                "agent": agent,
                "type": "recovery",
                "fix": successful_cmd[:200],
            }) + "\n")
    except OSError:
        pass


def _try_collapse_plan(plan: Plan) -> Plan:
    """Collapse sequential all-script same-pane plans into a single subtask.

    If all subtasks are script mode, on the same pane, in a linear chain
    (each depends only on the previous), merge them into one subtask with
    a combined description. This turns 3 LLM calls into 1.
    """
    subtasks = plan.subtasks
    if len(subtasks) <= 1:
        return plan

    # Check: all script mode?
    if not all(s.mode == "script" for s in subtasks):
        return plan

    # Check: all same pane?
    panes = {s.pane for s in subtasks}
    if len(panes) > 1:
        return plan

    # Check: linear chain? (each depends only on the previous)
    for i, s in enumerate(subtasks):
        if i == 0:
            if s.depends_on:
                return plan  # first has deps
        else:
            expected_dep = subtasks[i - 1].id
            if s.depends_on != [expected_dep]:
                return plan  # not a simple chain

    # Collapse: merge descriptions into one subtask
    merged_desc = " Then: ".join(
        f"Step {i+1}: {s.description}" for i, s in enumerate(subtasks)
    )
    progress(f"  COMPILE: collapsed {len(subtasks)} script subtasks into 1")

    collapsed = Plan(task=plan.task, subtasks=[
        Subtask(
            id="compiled",
            description=merged_desc,
            pane=subtasks[0].pane,
            mode="script",
            max_turns=max(s.max_turns for s in subtasks),
        ),
    ])
    return collapsed


def _build_plan_context(plan: Plan, current: Subtask) -> str:
    """Build a brief plan summary so the agent knows its role."""
    total = len(plan.subtasks)
    idx = next((i for i, s in enumerate(plan.subtasks) if s.id == current.id), 0) + 1
    parallel = [s for s in plan.subtasks if not s.depends_on and s.id != current.id]
    dependents = [s for s in plan.subtasks if current.id in s.depends_on]

    parts = [f"[Plan: \"{plan.task[:60]}\" — subtask {idx} of {total}]"]
    if parallel:
        parts.append(f"[Parallel: {', '.join(s.id + ':' + s.pane for s in parallel[:3])}]")
    if dependents:
        parts.append(f"[Downstream: {', '.join(s.id + ' needs your output' for s in dependents[:2])}]")
    return "\n".join(parts)


def _capture_pane_env(pane_info: PaneInfo, session_dir: str) -> str:
    """Capture pane environment state cheaply (no LLM call)."""
    # Get pane dimensions
    try:
        w = pane_info.pane.cmd("display-message", "-p", "#{pane_width}").stdout[0]
        h = pane_info.pane.cmd("display-message", "-p", "#{pane_height}").stdout[0]
        dims = f", {w}x{h}"
    except Exception:
        dims = ""
    return f"[Pane: {pane_info.name} [{pane_info.app_type}]{dims}, session_dir={session_dir}]"


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
    plan_context: str = "",
    tokens_used: int = 0,
    max_tokens: int = 50000,
    all_panes: dict | None = None,
) -> SubtaskResult:
    """Execute a single subtask. Dispatches based on observation level (mode)."""
    # Check for executable skill: if description contains [skill:name] and the skill
    # has STEPS, run mechanically (zero LLM calls on happy path)
    import re as _re
    _skill_match = _re.search(r'\[skill:([\w-]+(?:\s+\w+=\S+)*)\]', subtask.description)
    if _skill_match:
        from skills import load_skill, resolve_skill_with_params, inject_params
        from skill_runner import parse_executable_steps, run_executable_skill
        skill_ref = _skill_match.group(1)
        skill_name, params = resolve_skill_with_params(skill_ref)
        skill_content = load_skill(skill_name)
        if skill_content:
            skill_content = inject_params(skill_content, params)
            steps = parse_executable_steps(skill_content)
            if steps:
                progress(f"    [{subtask.id}] Executable skill: {skill_name} ({len(steps)} steps)")
                return run_executable_skill(
                    steps=steps,
                    pane_info=pane_info,
                    session_dir=session_dir,
                    params=params,
                    subtask_id=subtask.id,
                )

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

    # Smart max_turns: mode-aware defaults when planner didn't specify
    _MODE_TURNS = {"script": 3, "interactive": 8, "streaming": 10}
    if subtask.max_turns == 15:  # default wasn't overridden
        subtask.max_turns = _MODE_TURNS.get(subtask.mode, 8)

    log.info(f"Subtask {subtask.id}: mode={subtask.mode}, pane={subtask.pane}, max_turns={subtask.max_turns}")

    # Interactive/streaming mode: turn-by-turn observation loop
    client = get_client()
    total_pt = 0
    last_cmd_had_error = False  # for error recovery sharing
    total_ct = 0

    # Build enriched system prompt with plan context and pane environment
    pane_env = _capture_pane_env(pane_info, session_dir)
    system_prompt = build_worker_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )
    if plan_context:
        system_prompt += f"\n\n{plan_context}"
    system_prompt += f"\n{pane_env}"

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
    NO_CHANGE_LIMIT = 3
    skip_capture = False  # skip capture after file ops (screen unchanged)

    with _pane_locks[subtask.pane]:
        for turn in range(1, subtask.max_turns + 1):
            # Capture current pane state (skip if last action was a file op)
            if not skip_capture:
                screen = capture_pane(pane_info)
            skip_capture = False

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

            screen_content = compute_screen_diff(last_screen, screen)
            last_screen = screen

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

            # Turn progress + budget awareness
            budget_remaining = max_tokens - tokens_used - total_pt - total_ct
            budget_note = f"\n[Budget: {budget_remaining:,} tokens remaining]" if budget_remaining < max_tokens * 0.5 else ""
            turn_stats = f"[Turn {turn}/{subtask.max_turns} | {total_pt+total_ct:,} tokens used]"

            context = (
                f"{turn_stats}\n"
                f"[Pane: {subtask.pane}]\n{screen_content}"
                f"{scratchpad_note}{budget_note}"
            )
            messages.append({"role": "user", "content": context})

            # Trim context to prevent unbounded growth
            messages = _trim_messages(messages, max_user_turns=4)

            # Progressive prompt thinning: after turn 1, use minimal system prompt
            # (for non-caching providers; Anthropic caches automatically)
            if turn > 1 and len(messages) > 0 and messages[0]["role"] == "system":
                if len(messages[0]["content"]) > 200:
                    messages[0] = {"role": "system", "content": (
                        f"Continue task on pane {subtask.pane}. Pipeline commands OK. "
                        f"Auto-verify active. Exit codes captured. Session: {session_dir}"
                    )}

            # Call LLM with streaming for early command detection
            cmd_start = time.time()
            early_cmd = None
            early_cmd_event = threading.Event()

            def _on_stream_token(partial):
                nonlocal early_cmd
                # Detect first </cmd> in stream for early action
                if early_cmd is None and "</cmd>" in partial:
                    early_cmd = parse_command(partial)
                    if early_cmd["type"] != "none":
                        early_cmd_event.set()

            try:
                reply, pt, ct = chat_stream(client, messages, on_token=_on_stream_token)
            except Exception:
                # Fallback to synchronous if streaming fails
                reply, pt, ct = chat(client, messages)

            total_pt += pt
            total_ct += ct
            messages.append({"role": "assistant", "content": reply})

            progress(f"    [{subtask.id}] Turn {turn}: {reply[:80]}...")

            # Parse commands — support pipelining (multiple commands per LLM call)
            cmds = parse_commands(reply)

            # Emit turn event
            cmd_snippet = cmds[0]["value"][:80] if cmds[0]["value"] else cmds[0]["type"]
            if len(cmds) > 1:
                cmd_snippet += f" (+{len(cmds)-1} more)"
            _emit(on_event, "turn", subtask.id, turn, cmd_snippet)
            _emit(on_event, "tokens", subtask.id, pt, ct)

            # Execute commands in pipeline order; stop on first requiring LLM feedback
            pipeline_broke = False
            for cmd_idx, cmd in enumerate(cmds):
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
                    violation = _check_command_safety(cmd["value"])
                    if violation:
                        log.warning(violation)
                        messages.append({"role": "user", "content": f"[BLOCKED] {violation}. Choose a safer approach."})
                        pipeline_broke = True
                        break

                    _SHELL_LIKE = {"shell", "data", "docs", "media", "browser", "files"}
                    cmd_t0 = time.time()
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
                    cmd_elapsed = time.time() - cmd_t0

                    # Parse exit code from marker line
                    exit_code = None
                    for line in screen.splitlines():
                        if "EXIT:" in line and "___DONE_" in line:
                            try:
                                exit_code = int(line.split("EXIT:")[1].split()[0])
                            except (ValueError, IndexError):
                                pass

                    # Command echo with timing + exit code
                    echo_parts = [f"[Command executed: {cmd['value'][:100]}]"]
                    if exit_code is not None:
                        echo_parts.append(f"[Exit: {exit_code} | {cmd_elapsed:.1f}s | {method}]")
                    else:
                        echo_parts.append(f"[{cmd_elapsed:.1f}s | {method}]")

                    signal = _detect_outcome_signal(screen)
                    if signal:
                        echo_parts.append(f"[Outcome: {signal}]")

                    auto_verify = _auto_verify_command(cmd["value"], session_dir)
                    if auto_verify:
                        echo_parts.append(f"[Verified: {auto_verify}]")

                    current_has_error = signal == "error indicators detected"
                    if last_cmd_had_error and not current_has_error:
                        _write_recovery_pattern(session_dir, subtask.pane, cmd["value"])
                    last_cmd_had_error = current_has_error

                    messages.append({"role": "user", "content": "\n".join(echo_parts)})

                    # Pipeline break: if command failed or needs intervention, stop pipeline
                    if method.startswith("intervention:"):
                        intervention_type = method.split(":", 1)[1]
                        messages.append({
                            "role": "user",
                            "content": f"[INTERVENTION DETECTED: {intervention_type}] "
                                       f"The command needs your input. Screen:\n{screen}",
                        })
                        pipeline_broke = True
                        break
                    if exit_code is not None and exit_code != 0:
                        pipeline_broke = True
                        break  # stop pipeline, let LLM see the error

                elif cmd["type"] == "read_file":
                    content = read_file(cmd["value"])
                    messages.append({"role": "user", "content": content})
                    no_change_count = 0
                    skip_capture = True

                elif cmd["type"] == "write_file":
                    result = write_file(cmd["path"], cmd["value"])
                    messages.append({"role": "user", "content": result})
                    no_change_count = 0
                    skip_capture = True

                elif cmd["type"] == "peek":
                    target_pane = cmd.get("pane") or cmd.get("value", "").strip()
                    if all_panes and target_pane in all_panes:
                        peek_screen = capture_pane(all_panes[target_pane])
                        messages.append({
                            "role": "user",
                            "content": f"[Peek at pane {target_pane}]:\n{peek_screen[-500:]}",
                        })
                    else:
                        messages.append({
                            "role": "user",
                            "content": f"[Peek failed: pane '{target_pane}' not found]",
                        })
                    no_change_count = 0

                elif cmd["type"] == "wait":
                    wait_secs = 2
                    try:
                        wait_secs = max(1, min(int(cmd["value"]), 10))
                    except (ValueError, TypeError):
                        pass
                    progress(f"    [{subtask.id}] Waiting {wait_secs}s...")
                    time.sleep(wait_secs)

                elif cmd["type"] == "save_skill":
                    # Agent creates a new skill for future reuse
                    try:
                        from skills import save_skill
                        # value format: "name: content..." or just content with name from pane
                        if ":" in cmd["value"][:30]:
                            skill_name, skill_body = cmd["value"].split(":", 1)
                            skill_name = skill_name.strip()
                            skill_body = skill_body.strip()
                        else:
                            skill_name = f"learned_{subtask.id}"
                            skill_body = cmd["value"]
                        path = save_skill(skill_name, skill_body)
                        messages.append({"role": "user", "content": f"[Skill saved: {skill_name} → {path}]"})
                        progress(f"    [{subtask.id}] Saved skill: {skill_name}")
                    except Exception as e:
                        messages.append({"role": "user", "content": f"[Skill save failed: {e}]"})
                    no_change_count = 0

                elif cmd["type"] == "none":
                    pass  # no command, next turn will observe screen

            # If pipeline broke (error/intervention), stop executing remaining commands
            if pipeline_broke:
                break

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
