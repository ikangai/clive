"""DAG scheduler and per-subtask worker execution."""

import json
import logging
import os
import re
import shlex
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, Future

log = logging.getLogger(__name__)

from output import progress
from models import Plan, Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from completion import wait_for_ready, wrap_command
from llm import get_client, chat, SCRIPT_MODEL
from session import capture_pane
from screen_diff import compute_screen_diff
from prompts import build_script_prompt

# Per-pane locks: only one subtask can use a pane at a time
_pane_locks: dict[str, threading.Lock] = {}

# Global cancellation event — set by signal handler to abort all workers
_cancel_event = threading.Event()


def cancel():
    """Signal all workers to stop."""
    _cancel_event.set()


def is_cancelled() -> bool:
    """Check if cancellation has been requested."""
    return _cancel_event.is_set()


def reset_cancel():
    """Reset cancellation state for a new run."""
    _cancel_event.clear()

# ─── Command Safety ──────────────────────────────────────────────────────────

BLOCKED_COMMANDS = [
    re.compile(r'rm\s+(-\w*\s+)*-r[f ]\s+/\s*$'),
    re.compile(r'rm\s+(-\w*\s+)*-rf\s+(~|\$HOME|/home)\b'),
    re.compile(r'\b(shutdown|reboot|halt|poweroff)\b'),
    re.compile(r'\bmkfs\b'),
    re.compile(r'\bdd\s+.*of=/dev/'),
    re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'),  # fork bomb
    re.compile(r'>\s*/dev/sd[a-z]'),
    re.compile(r'chmod\s+(-\w+\s+)*777\s+/\s*$'),
    re.compile(r'\bwhile\s+true\s*;\s*do\s*:?\s*;?\s*done'),
    re.compile(r'\beval\s+"?\$\(.*base64'),
]


def _check_command_safety(command: str) -> str | None:
    """Check command against blocklist. Returns violation or None."""
    for pattern in BLOCKED_COMMANDS:
        if pattern.search(command):
            return f"Blocked dangerous command: {command[:80]}"
    return None


# ─── Sandbox Wrapping ───────────────────────────────────────────────────────

def _wrap_for_sandbox(cmd: str, session_dir: str, sandboxed: bool = False, no_network: bool = False) -> str:
    """Wrap a command through the sandbox script if sandboxing is enabled."""
    if not sandboxed and os.environ.get("CLIVE_SANDBOX") != "1":
        return cmd
    script = os.path.join(os.path.dirname(__file__), "sandbox", "run.sh")
    parts = ["bash", shlex.quote(script), shlex.quote(session_dir)]
    if no_network:
        parts.append("--no-network")
    parts.append(shlex.quote(cmd))
    return " ".join(parts)









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


# ─── Direct Mode Worker ──────────────────────────────────────────────────────

def run_subtask_direct(
    subtask: Subtask,
    pane_info: PaneInfo,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Run a literal shell command directly — zero LLM calls.

    Uses file-based output capture instead of screen scraping to handle
    commands with large output (e.g. curl) that would scroll the marker
    off the visible tmux pane.
    """
    import uuid as _uuid

    cmd = subtask.description.strip()
    cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)
    nonce = _uuid.uuid4().hex[:4]
    marker = f"___DONE_{subtask.id}_{nonce}___"
    out_file = os.path.join(session_dir, f"_direct_{subtask.id}.out")
    ec_file = os.path.join(session_dir, f"_direct_{subtask.id}.ec")

    # Redirect output to file, write exit code to file, then echo marker
    combined = f'{cmd} > {out_file} 2>&1; echo $? > {ec_file}; echo "{marker}"'

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
    with lock:
        pane_info.pane.send_keys(combined, enter=True)
        time.sleep(0.15)  # Let shell start processing before polling
        wait_for_ready(pane_info, marker=marker, max_wait=30.0)

    # Read exit code from file
    exit_code = 0
    try:
        with open(ec_file) as f:
            exit_code = int(f.read().strip())
    except (OSError, ValueError):
        pass

    # Read output from file
    output = ""
    try:
        with open(out_file, errors="replace") as f:
            output = f.read()
    except OSError:
        pass

    summary = output.strip()[-2000:] if output.strip() else "Done (no output)"
    status = SubtaskStatus.COMPLETED if exit_code == 0 else SubtaskStatus.FAILED

    return SubtaskResult(
        subtask_id=subtask.id,
        status=status,
        summary=summary,
        output_snippet=output[-500:] if len(output) > 500 else output,
        turns_used=1,
        exit_code=exit_code,
    )


# ─── Script Mode Worker ─────────────────────────────────────────────────────

def _resolve_script_language(script: str, session_dir: str, subtask_id: str, default_path: str) -> tuple[str, str]:
    """Detect script language from shebang. Returns (script_path, executor)."""
    if script.startswith("#!/usr/bin/env python") or script.startswith("#!/usr/bin/python"):
        return os.path.join(session_dir, f"_script_{subtask_id}.py"), "python3"
    return default_path, "bash"


def _audit_script_generation(subtask_id: str, attempt: int, script_path: str) -> None:
    """Best-effort audit logging — failures don't block execution."""
    try:
        from selfmod.audit import log_attempt
        log_attempt(
            proposal_id=f"script_{subtask_id}_{attempt}",
            action="script_generate",
            files=[script_path],
            tier="OPEN",
            roles={},
            gate_result={"allowed": True, "reason": "script generation"},
            outcome="generated",
            details=f"Script for subtask {subtask_id}, attempt {attempt}",
        )
    except Exception:
        pass


def _execute_script_in_pane(pane_info: PaneInfo, script_executor: str, script_path: str,
                            session_dir: str, subtask_id: str) -> tuple[str, int | None, str]:
    """Run a script in the pane and parse its exit code. Returns (screen, exit_code, nonce)."""
    nonce = uuid.uuid4().hex[:4]
    marker = f"___DONE_{subtask_id}_{nonce}___"
    script_cmd = _wrap_for_sandbox(
        f'{script_executor} {script_path}', session_dir, sandboxed=pane_info.sandboxed,
    )
    combined = f'{script_cmd}; echo "EXIT:$? {marker}"'
    pane_info.pane.send_keys(combined, enter=True)
    screen, _method = wait_for_ready(pane_info, marker=marker, max_wait=60.0)
    logging.debug(f"[{subtask_id}] Script attempt: {screen[-80:]}")

    exit_code: int | None = None
    for line in screen.splitlines():
        if marker in line and "EXIT:" in line:
            try:
                exit_code = int(line.split("EXIT:")[1].split()[0])
            except (ValueError, IndexError):
                pass
    return screen, exit_code, nonce


def _extract_script_output(screen: str, nonce: str, script_path: str, session_dir: str) -> str:
    """Strip markers, prompts and command echoes from the captured screen output."""
    nonce_frag = nonce + "___"
    output_lines = [
        l for l in screen.splitlines()
        if l.strip()
        and nonce_frag not in l
        and "___DONE_" not in l
        and "AGENT_READY" not in l
        and "export PS1=" not in l
        and not l.strip().startswith("EXIT:")
    ]
    cmd_echo = os.path.basename(script_path)
    while output_lines and (
        output_lines[0].strip().startswith(("$ bash ", "$ sh ", "$ python "))
        or output_lines[0].strip().startswith("$ /")
        or cmd_echo in output_lines[0]
        or output_lines[0].strip() == session_dir
        or output_lines[0].strip() == os.path.basename(session_dir)
    ):
        output_lines.pop(0)
    return "\n".join(output_lines)[-2000:] if output_lines else "Done (no output)"


def _write_script_success_artifacts(session_dir: str, subtask_id: str, summary: str,
                                    attempt: int, screen: str) -> None:
    """Persist the success result.json and execution log."""
    result_path = os.path.join(session_dir, f"_result_{subtask_id}.json")
    write_file(result_path, json.dumps({
        "status": "success",
        "subtask_id": subtask_id,
        "summary": summary,
        "turns_used": attempt,
    }, indent=2))
    log_path = os.path.join(session_dir, f"_log_{subtask_id}.txt")
    write_file(log_path, screen)


def run_subtask_script(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute a subtask in script mode: generate → execute → verify → repair loop."""
    log.info(f"Subtask {subtask.id}: script mode, pane={subtask.pane}")
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
    default_script_path = os.path.join(session_dir, f"_script_{subtask.id}.sh")

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
    with lock:
        for attempt in range(1, subtask.max_turns + 1):
            if _cancel_event.is_set():
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary="Cancelled", output_snippet="",
                    turns_used=attempt - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )
            reply, pt, ct = chat(client, messages, model=SCRIPT_MODEL)
            total_pt += pt
            total_ct += ct
            messages.append({"role": "assistant", "content": reply})
            _emit(on_event, "turn", subtask.id, attempt, f"script gen attempt {attempt}")
            _emit(on_event, "tokens", subtask.id, pt, ct)

            try:
                script = _extract_script(reply)
            except ValueError as e:
                logging.debug(f"[{subtask.id}] Script extraction failed: {e}")
                messages.append({"role": "user", "content": "Error: could not extract script. Respond with a bash script inside ```bash ``` markers."})
                continue

            script_path, script_executor = _resolve_script_language(
                script, session_dir, subtask.id, default_script_path,
            )
            write_file(script_path, script)
            os.chmod(script_path, 0o755)
            _audit_script_generation(subtask.id, attempt, script_path)

            screen, exit_code, nonce = _execute_script_in_pane(
                pane_info, script_executor, script_path, session_dir, subtask.id,
            )

            if exit_code == 0:
                summary = _extract_script_output(screen, nonce, script_path, session_dir)
                _write_script_success_artifacts(session_dir, subtask.id, summary, attempt, screen)
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
            logging.debug(f"[{subtask.id}] Script failed (exit {exit_code}), repairing...")
            messages.append({
                "role": "user",
                "content": f"Script failed with exit code {exit_code}. Terminal output:\n\n{screen[-1000:]}\n\nFix the script and provide the corrected version.",
            })

    final_screen = capture_pane(pane_info)
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


class _ExecState:
    """Shared mutable state for the DAG scheduler loop.

    A small namespace bundle so the submit/collect/deadlock helpers can share
    state without a 10-parameter signature. Only used inside execute_plan.
    """
    __slots__ = (
        "plan", "panes", "on_event", "session_dir", "max_tokens",
        "results", "futures", "subtask_map", "start_times", "result_files",
        "wake_event", "pool",
    )

    def __init__(self, plan, panes, on_event, session_dir, max_tokens, pool, wake_event):
        self.plan = plan
        self.panes = panes
        self.on_event = on_event
        self.session_dir = session_dir
        self.max_tokens = max_tokens
        self.pool = pool
        self.wake_event = wake_event
        self.results: dict[str, SubtaskResult] = {}
        self.futures: dict[str, Future] = {}
        self.subtask_map = {s.id: s for s in plan.subtasks}
        self.start_times: dict[str, float] = {}
        self.result_files: dict[str, list[str]] = {}


def _cancel_all_pending(state: "_ExecState") -> None:
    """Mark every unfinished subtask as FAILED(Cancelled) and cancel its future."""
    for sid in list(state.futures.keys()):
        state.futures[sid].cancel()
    for s in state.plan.subtasks:
        if s.id not in state.results:
            state.results[s.id] = SubtaskResult(
                subtask_id=s.id, status=SubtaskStatus.FAILED,
                summary="Cancelled", output_snippet="",
            )


def _submit_ready_subtasks(state: "_ExecState") -> None:
    """Walk the DAG and submit every subtask whose dependencies are satisfied."""
    for subtask in state.plan.subtasks:
        if subtask.id in state.futures or subtask.id in state.results:
            continue

        # Check if any dependency failed → skip
        deps_failed = any(
            dep_id in state.results
            and state.results[dep_id].status in (SubtaskStatus.FAILED, SubtaskStatus.SKIPPED)
            for dep_id in subtask.depends_on
        )
        if deps_failed:
            state.results[subtask.id] = SubtaskResult(
                subtask_id=subtask.id,
                status=SubtaskStatus.SKIPPED,
                summary="Skipped: dependency failed",
                output_snippet="",
            )
            subtask.status = SubtaskStatus.SKIPPED
            logging.debug(f"SKIP [{subtask.id}] dependency failed")
            _emit(state.on_event, "subtask_skip", subtask.id, "dependency failed")
            continue

        # Check all dependencies completed
        deps_met = all(
            dep_id in state.results and state.results[dep_id].status == SubtaskStatus.COMPLETED
            for dep_id in subtask.depends_on
        )
        if not deps_met:
            continue

        # Build enriched dependency context (with file registry)
        dep_context = _build_dependency_context(subtask, state.results, state.result_files)

        # Submit to thread pool
        subtask.status = SubtaskStatus.RUNNING
        state.start_times[subtask.id] = time.time()
        logging.debug(f"START [{subtask.id}] [{subtask.pane}] {subtask.description[:60]}")
        _emit(state.on_event, "subtask_start", subtask.id, subtask.pane, subtask.description)
        future = state.pool.submit(
            run_subtask,
            subtask=subtask,
            pane_info=state.panes[subtask.pane],
            dep_context=dep_context,
            on_event=state.on_event,
            session_dir=state.session_dir,
        )
        future.add_done_callback(lambda _f, ev=state.wake_event: ev.set())
        state.futures[subtask.id] = future


def _enforce_token_budget(state: "_ExecState") -> bool:
    """Cancel pending work if token spend exceeds budget. Returns True if triggered."""
    total_tokens = sum(r.prompt_tokens + r.completion_tokens for r in state.results.values())
    if total_tokens <= state.max_tokens:
        return False
    progress(f"  TOKEN BUDGET EXCEEDED: {total_tokens:,} > {state.max_tokens:,}")
    for remaining_sid in list(state.futures.keys()):
        state.futures[remaining_sid].cancel()
        del state.futures[remaining_sid]
    for s in state.plan.subtasks:
        if s.id not in state.results:
            state.results[s.id] = SubtaskResult(
                subtask_id=s.id,
                status=SubtaskStatus.SKIPPED,
                summary="Skipped: token budget exceeded",
                output_snippet="",
            )
    return True


def _collect_completed_futures(state: "_ExecState") -> tuple[bool, bool]:
    """Drain finished futures. Returns (collected_any, budget_exceeded)."""
    collected_any = False
    for sid in list(state.futures.keys()):
        future = state.futures[sid]
        if not future.done():
            continue
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
        subtask_obj = state.subtask_map[sid]
        if (result.status == SubtaskStatus.FAILED
                and subtask_obj.mode == "script"
                and not subtask_obj._retried):
            logging.debug(f"RETRY [{sid}] script failed, retrying as interactive")
            _emit(state.on_event, "subtask_fail", sid, f"script failed, retrying interactive")
            subtask_obj.mode = "interactive"
            subtask_obj.max_turns = max(subtask_obj.max_turns, 10)
            subtask_obj._retried = True
            subtask_obj.status = SubtaskStatus.PENDING
            del state.futures[sid]
            collected_any = True
            continue

        state.results[sid] = result
        state.subtask_map[sid].status = result.status
        elapsed = time.time() - state.start_times.get(sid, time.time())
        logging.debug(f"{'DONE' if result.status == SubtaskStatus.COMPLETED else 'FAIL'} [{sid}] {result.summary[:60]}")

        if result.status == SubtaskStatus.COMPLETED:
            _emit(state.on_event, "subtask_done", sid, result.summary, elapsed)
            # Scan session dir for files written by this subtask
            _track_result_files(sid, state.session_dir, state.result_files, result)
        else:
            _emit(state.on_event, "subtask_fail", sid, result.summary)
            # Branch cancellation: cancel futures whose results are now useless
            _cancel_orphaned_branches(sid, state.plan, state.results, state.futures, state.on_event)

        del state.futures[sid]
        collected_any = True

        # Token budget enforcement
        if _enforce_token_budget(state):
            return collected_any, True

    return collected_any, False


def _mark_deadlocked(state: "_ExecState") -> None:
    """Mark unresolved subtasks as FAILED(Deadlocked) when no futures remain."""
    unresolved = [s.id for s in state.plan.subtasks if s.id not in state.results]
    if not unresolved:
        return
    progress(f"  WARNING: Deadlocked — no running subtasks, {unresolved} unresolved")
    for sid in unresolved:
        state.results[sid] = SubtaskResult(
            subtask_id=sid,
            status=SubtaskStatus.FAILED,
            summary="Deadlocked: could not start",
            output_snippet="",
        )
        _emit(state.on_event, "subtask_fail", sid, "Deadlocked: could not start")


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

    # Per-plan pane locks (scoped to this execution only)
    plan_locks = {pane_name: threading.Lock() for pane_name in panes}
    _pane_locks.clear()
    _pane_locks.update(plan_locks)

    panes_used = {s.pane for s in plan.subtasks}
    max_workers = max(len(panes_used), 1)
    wake_event = threading.Event()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        state = _ExecState(plan, panes, on_event, session_dir, max_tokens, pool, wake_event)
        while True:
            if _cancel_event.is_set():
                _cancel_all_pending(state)
                break

            _submit_ready_subtasks(state)
            collected_any, budget_exceeded = _collect_completed_futures(state)
            if budget_exceeded:
                break

            if len(state.results) == len(plan.subtasks):
                break

            if collected_any:
                wake_event.clear()
                continue

            if not state.futures:
                _mark_deadlocked(state)
                break

            wake_event.wait(timeout=0.5)
            wake_event.clear()

    return [state.results[s.id] for s in plan.subtasks]




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
    collapsible_ids = {s.id for s in subtasks}
    for i, s in enumerate(subtasks):
        if i == 0:
            if s.depends_on:
                return plan  # first has deps
        else:
            expected_dep = subtasks[i - 1].id
            if s.depends_on != [expected_dep]:
                return plan  # not a simple chain
        # Check no external subtask depends on an internal one (except the last)
        # This is checked at the Plan level, so collapsing is only safe for
        # standalone chains. (Plan only has these subtasks, so it's always safe.)

    # Collapse: merge descriptions into one subtask
    merged_desc = " Then: ".join(
        f"Step {i+1}: {s.description}" for i, s in enumerate(subtasks)
    )
    logging.debug(f"COMPILE: collapsed {len(subtasks)} script subtasks into 1")

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
    """Cancel subtasks that transitively depend on a failed subtask.

    Walks the full dependency graph: if A fails, and B depends on A,
    and C depends on B, then both B and C are cancelled.
    """
    # Build adjacency: parent → children
    children: dict[str, list[str]] = {s.id: [] for s in plan.subtasks}
    for s in plan.subtasks:
        for dep in s.depends_on:
            if dep in children:
                children[dep].append(s.id)

    # BFS from failed_sid to find all transitive dependents
    unreachable: set[str] = set()
    queue = [failed_sid]
    while queue:
        current = queue.pop(0)
        for child in children.get(current, []):
            if child not in unreachable and child not in results:
                unreachable.add(child)
                queue.append(child)

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
            logging.debug(f"CANCEL [{sid}] branch cancelled (dep {failed_sid} failed)")
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


# ─── V2 Interactive Worker ────────────────────────────────────────────────────

def run_subtask_interactive(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute a subtask via the read-think-type loop.

    The LLM reads the pane screen, outputs a shell command as plain text,
    and the executor types it into the pane. No XML protocol, no side channels.
    The pane scrollback IS the session store.
    """
    from command_extract import extract_command, extract_done
    from prompts import build_interactive_prompt

    client = get_client()
    total_pt = total_ct = 0
    empty_reply_count = 0
    EMPTY_REPLY_LIMIT = 2

    system_prompt = build_interactive_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Begin. Goal: {subtask.description}"},
    ]

    prev_screen = None

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
    with lock:
        for turn in range(1, subtask.max_turns + 1):
            if _cancel_event.is_set():
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary="Cancelled", output_snippet="",
                    turns_used=turn - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            screen = capture_pane(pane_info)
            diff = compute_screen_diff(prev_screen, screen)
            prev_screen = screen

            messages.append({"role": "user", "content": diff})
            messages = _trim_messages(messages)

            reply, pt, ct = chat(client, messages)
            total_pt += pt
            total_ct += ct

            # Detect consecutive empty replies (LLM outage / broken provider)
            if not reply.strip():
                empty_reply_count += 1
                if empty_reply_count >= EMPTY_REPLY_LIMIT:
                    return SubtaskResult(
                        subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                        summary=f"LLM returned {EMPTY_REPLY_LIMIT} consecutive empty responses",
                        output_snippet=screen[-500:] if screen else "",
                        turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                    )
                continue
            empty_reply_count = 0

            messages.append({"role": "assistant", "content": reply})

            _emit(on_event, "turn", subtask.id, turn, reply[:80])
            _emit(on_event, "tokens", subtask.id, pt, ct)

            # Check completion
            done = extract_done(reply)
            if done is not None:
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
                    summary=done, output_snippet=screen[-500:],
                    turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            # Extract and execute command
            cmd = extract_command(reply)
            if not cmd:
                continue  # no command, next turn observes screen

            violation = _check_command_safety(cmd)
            if violation:
                log.warning(violation)
                messages.append({"role": "user", "content": f"[BLOCKED] {violation}. Try a different approach."})
                continue

            # Sandbox wrapping for shell-like panes
            _SHELL_LIKE = {"shell", "data", "docs", "media", "browser", "files"}
            if pane_info.app_type in _SHELL_LIKE:
                cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)

            wrapped, marker = wrap_command(cmd, subtask.id)
            pane_info.pane.send_keys(wrapped, enter=True)
            screen, method = wait_for_ready(pane_info, marker=marker)
            prev_screen = screen  # update for next diff

    # Exhausted turns
    final_screen = capture_pane(pane_info)
    return SubtaskResult(
        subtask_id=subtask.id, status=SubtaskStatus.FAILED,
        summary=f"Exhausted {subtask.max_turns} turns without completing",
        output_snippet=final_screen[-500:],
        turns_used=subtask.max_turns, prompt_tokens=total_pt, completion_tokens=total_ct,
    )


# ─── Per-Subtask Worker ───────────────────────────────────────────────────────

def run_subtask(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
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
                logging.debug(f"[{subtask.id}] Executable skill: {skill_name} ({len(steps)} steps)")
                return run_executable_skill(
                    steps=steps,
                    pane_info=pane_info,
                    session_dir=session_dir,
                    params=params,
                    subtask_id=subtask.id,
                )

    if subtask.mode == "direct":
        return run_subtask_direct(
            subtask=subtask,
            pane_info=pane_info,
            on_event=on_event,
            session_dir=session_dir,
        )

    if subtask.mode == "script":
        return run_subtask_script(
            subtask=subtask,
            pane_info=pane_info,
            dep_context=dep_context,
            on_event=on_event,
            session_dir=session_dir,
        )

    # Smart max_turns: mode-aware defaults when planner didn't specify
    _MODE_TURNS = {"script": 3, "interactive": 8, "streaming": 10}
    if subtask.max_turns == 15:  # default wasn't overridden
        subtask.max_turns = _MODE_TURNS.get(subtask.mode, 8)

    # Interactive and streaming modes → v2 worker
    return run_subtask_interactive(
        subtask=subtask,
        pane_info=pane_info,
        dep_context=dep_context,
        on_event=on_event,
        session_dir=session_dir,
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
