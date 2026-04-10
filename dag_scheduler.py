"""DAG scheduler — executes a Plan across parallel panes, respecting dependencies.

Extracted from executor.py. Runs one worker per pane via ThreadPoolExecutor,
submits ready subtasks as dependencies complete, tracks result files and
token budget, and cancels orphaned branches on failure.

Shares primitives with executor.py via a deferred `import executor`:
- executor.run_subtask       (worker dispatched to the thread pool)
- executor._pane_locks       (per-pane mutex table)
- executor._cancel_event     (global cancellation signal)
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future

import executor  # deferred attribute access avoids the circular import
from models import Plan, Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from output import progress

log = logging.getLogger(__name__)


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
            executor.run_subtask,
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
    executor._pane_locks.clear()
    executor._pane_locks.update(plan_locks)

    panes_used = {s.pane for s in plan.subtasks}
    max_workers = max(len(panes_used), 1)
    wake_event = threading.Event()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        state = _ExecState(plan, panes, on_event, session_dir, max_tokens, pool, wake_event)
        while True:
            if executor._cancel_event.is_set():
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
