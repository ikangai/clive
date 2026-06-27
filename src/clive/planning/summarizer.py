"""Result synthesis, session logging, and failure recovery.

Extracted from clive.py to isolate the post-execution phase:
    - attempt_recovery(): rerun failed subtasks via a fresh planner call
    - read_output_files(): prefer user-created files over raw terminal output
    - summarize(): final LLM call to synthesize all subtask results
    - log_session(): append structured record to the cross-run session log
"""

import json
import logging
import os
import time

from file_inspect import sniff_session_files
from llm import CLASSIFIER_MODEL, chat, get_client
from models import SubtaskStatus
from output import detail, step
from planner import create_plan
from prompts import build_summarizer_prompt, wrap_untrusted

log = logging.getLogger(__name__)

SESSION_LOG = os.path.expanduser("~/.clive_session_log.jsonl")


def detect_false_completion(results):
    """Flag COMPLETED subtask results whose observable evidence contradicts them.

    A subtask can be marked COMPLETED while its evidence says otherwise — a false
    success that silently poisons the synthesized answer (Terminal-Bench, arXiv
    2601.11868: verification errors are ~25% of CLI-agent failures). This is a
    pure, deterministic check that uses ONLY fields already on SubtaskResult.

    PRIMARY signal: status is COMPLETED but ``exit_code`` is an int and != 0 —
    the agent claimed done, yet the last command failed. ``exit_code is None``
    (unknown) and ``exit_code == 0`` (supported success) are NOT flagged, so the
    check produces no false positives; FAILED/SKIPPED results are honest, not
    *false* completions, so they are skipped too.

    Returns a list of ``(subtask_id, reason)`` pairs (empty when nothing is
    suspect). Returning the flags — rather than mutating anything — lets the dead
    ``false_completion`` eval metric (#40) and the false-completion eval
    scenarios assert on them without touching the frozen execution/ or
    eval-harness code.
    """
    flags = []
    for r in results:
        if r.status != SubtaskStatus.COMPLETED:
            continue
        # bool is an int subclass; an exit_code should never be a bool, but
        # guard so a stray True/False can't masquerade as 1/0.
        if isinstance(r.exit_code, bool):
            continue
        if isinstance(r.exit_code, int) and r.exit_code != 0:
            flags.append((
                r.subtask_id,
                f"exit_code={r.exit_code}; DONE claim not supported by evidence",
            ))
    return flags


# ── Evidence-grounded DONE-verification judge (classifier-model) ──────────────
# detect_false_completion() above only catches a COMPLETED result that ALSO
# carries a *contradicting* exit_code. It is silent on the harder case the
# Terminal-Bench analysis flags most (arXiv 2601.11868): a result that exits 0
# (or unknown) yet whose summary does not actually satisfy the task — e.g.
# "created report.csv" when no such file was produced. For exactly those
# not-yet-flagged COMPLETED results we make ONE cheap classifier-model call that
# judges the DONE claim against OBSERVABLE evidence (the captured summary,
# terminal snippet, and output-file previews). The judge is deliberately an
# evidence grader, NOT free-form self-critique — pure self-critique can DEGRADE
# accuracy (ReVeal, VerifiAgent) — and it defaults to "supported" on any
# ambiguity so it can only ADD a caveat on positive evidence of failure, never
# manufacture a spurious one.

_JUDGE_SYSTEM = (
    "You are a strict verification judge for a CLI agent. A subtask was marked "
    "COMPLETED. Using ONLY the observable evidence provided (the agent's own "
    "summary, a terminal output snippet, and previews of any output files), "
    "decide whether that evidence SUPPORTS the claim that the subtask of the "
    "original task was actually completed. Do not assume work you cannot see. "
    "If the evidence is consistent with completion (or simply does not "
    "contradict it), it is supported. Call it unsupported ONLY when the evidence "
    "positively contradicts the claim or shows the requested result is missing. "
    'Reply with ONE JSON object and nothing else: '
    '{"supported": true|false, "reason": "<one short sentence>"}.'
)

_JUDGE_MAX_TOKENS = 200


def _evidence_block(result):
    """Render the OBSERVABLE evidence for one result — its claimed summary, a
    terminal output snippet, and short previews of any output files. This, not
    the model's own reasoning, is what the judge is grounded in."""
    parts = [f"Claimed summary: {result.summary or '(none)'}"]
    snippet = (result.output_snippet or "").strip()
    if snippet:
        parts.append(f"Terminal output snippet:\n{snippet[:1000]}")
    previews = []
    for f in result.output_files or []:
        path = f.get("path", "")
        preview = (f.get("preview") or "").strip()
        if path or preview:
            previews.append(f"  {path}: {preview[:200]}")
        if len(previews) >= 5:
            break
    if previews:
        parts.append("Output files:\n" + "\n".join(previews))
    return "\n\n".join(parts)


def _parse_judge_verdict(content):
    """Parse the classifier reply into ``(supported: bool, reason: str)``.

    Defaults to ``(True, "")`` on ANY ambiguity (unparseable reply, missing or
    non-bool ``supported`` field) so a flaky judge can never inject a spurious
    UNVERIFIED caveat — false-positive flags degrade the answer (ReVeal /
    VerifiAgent)."""
    text = (content or "").strip()
    # Strip a ```json fence if present (mirrors _classify in clive_core).
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("supported"), bool):
            return obj["supported"], str(obj.get("reason", "")).strip()
    return True, ""


def _judge_done_claim(client, task, result):
    """ONE cheap classifier-model call asking whether the OBSERVABLE evidence
    supports this COMPLETED result's DONE claim. Returns ``(supported, reason)``.
    Any failure (network error, garbled reply) is treated as SUPPORTED so the
    judge can never break the summary or remove a real success."""
    # The evidence quotes attacker-influenceable content (web text, file
    # previews), so wrap it as untrusted data — same posture as summarize().
    user = (
        f"Original task:\n{task}\n\n"
        + wrap_untrusted("DONE-CLAIM-EVIDENCE", _evidence_block(result))
    )
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        reply, _, _ = chat(
            client, messages, max_tokens=_JUDGE_MAX_TOKENS, model=CLASSIFIER_MODEL
        )
    except Exception as e:  # noqa: BLE001 — best-effort; never break the summary
        log.warning("done_judge subtask=%s call failed: %s", result.subtask_id, e)
        return True, ""
    return _parse_judge_verdict(reply)


def judge_false_completion(results, task, deterministic_flags, client=None):
    """Evidence-grounded classifier judge for the case detect_false_completion()
    misses: a COMPLETED result with a non-contradicting exit_code whose evidence
    still does not support the DONE claim.

    Runs the judge ONLY on COMPLETED results NOT already in *deterministic_flags*
    — a hard cost guard of at most one classifier call per such result — and
    skips entirely, making ZERO calls, when there are none. Returns extra
    ``(subtask_id, reason)`` flags (same shape as detect_false_completion) for
    results the judge deems UNSUPPORTED, so summarize() can merge them and the
    caveat/observability path stays identical for both gates.
    """
    if CLASSIFIER_MODEL == "none":  # classifier disabled — honor it (see _classify)
        return []
    already = {sid for sid, _ in deterministic_flags}
    candidates = [
        r for r in results
        if r.status == SubtaskStatus.COMPLETED and r.subtask_id not in already
    ]
    if not candidates:  # nothing to judge -> no model calls at all
        return []
    if client is None:
        client = get_client()
    flags = []
    for r in candidates:
        supported, reason = _judge_done_claim(client, task, r)
        if not supported:
            detail_reason = reason or "DONE claim not supported by observable evidence"
            flags.append((r.subtask_id, f"classifier judge: {detail_reason}"))
    return flags


def _build_result_text(results, flags):
    """Render the subtask-result block for the summarizer, annotating each
    flagged result's line with a grounded ``[UNVERIFIED — ...]`` caveat so the
    synthesized answer surfaces the discrepancy instead of laundering an
    unsupported success as fact."""
    caveats = {sid: reason for sid, reason in flags}
    lines = []
    for r in results:
        line = f"Subtask {r.subtask_id} [{r.status.value}]: {r.summary}"
        reason = caveats.get(r.subtask_id)
        if reason:
            line += f"\n  [UNVERIFIED — {reason}]"
        lines.append(line)
    return "\n\n".join(lines)


def attempt_recovery(task, results, plan_execute_fn, panes, tool_status,
                     tools_summary, on_event, session_dir, max_tokens):
    """When subtasks failed, replan with a fresh approach and retry once.

    Triggers whenever 1-2 subtasks failed — including the common autonomous
    case of a leaf/single-subtask failure that left no skipped dependents (the
    first approach exhausted its turns or hit an LLM error). The len(failed)<=2
    cap is kept because larger failures suggest fundamental planning problems
    that replanning won't fix, and recovery is a single, non-recursive attempt.
    Extends and returns the combined result list; on exception, returns the
    original results.
    """
    failed = [r for r in results if r.status == SubtaskStatus.FAILED]
    skipped = [r for r in results if r.status == SubtaskStatus.SKIPPED]
    if not (failed and len(failed) <= 2):
        return results

    step("Replanning")
    detail("Some subtasks failed, attempting recovery...")
    failure_context = "\n".join(
        f"  Subtask {r.subtask_id} FAILED: {r.summary}" for r in failed
    )
    replan_parts = [
        f"Original task: {task}\n",
        f"These subtasks failed:\n{failure_context}\n",
    ]
    if skipped:
        remaining = "\n".join(
            f"  Subtask {r.subtask_id} SKIPPED: {r.summary}" for r in skipped
        )
        replan_parts.append(f"These subtasks were skipped:\n{remaining}\n")
    replan_parts.append(
        "Find an alternative approach to complete the remaining work. "
        "Account for the failures — try a different method."
    )
    replan_task = "\n".join(replan_parts)
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

    # Evidence-grounded DONE-verification gate: before trusting any COMPLETED
    # result at face value, flag the ones whose observable evidence contradicts
    # success (e.g. non-zero exit_code) and annotate their lines so the
    # summarizer refuses to present an unverified success as fact. The retry
    # belongs to the runner; here we only refuse to launder it.
    flags = detect_false_completion(results)
    # The deterministic gate only catches a *contradicting* exit_code. Layer a
    # cheap evidence-grounded classifier judge over the COMPLETED results it did
    # NOT flag (exit_code 0/unknown) — one call each, none when there are none —
    # so a "done" claim that exits clean yet isn't supported by its evidence is
    # caught too. Judge flags share detect_false_completion's shape, so they flow
    # through the same caveat-annotation and observability path below.
    flags = flags + judge_false_completion(results, task, flags, client=client)
    if flags:
        for sid, reason in flags:
            log.warning("false_completion subtask=%s %s", sid, reason)
        detail(
            f"Unverified DONE claim(s) in {len(flags)} subtask(s) — "
            "flagged for the summary."
        )

    result_text = _build_result_text(results, flags)

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

    # Subtask summaries and file previews are attacker-influenceable (the
    # summary may quote a webpage; previews may contain prompt-injection
    # prose). Wrap them as untrusted data so the summarizer LLM (and any
    # downstream consumer of the summary) treats them as content, not
    # instructions. Audit H20 (2026-05-27).
    untrusted_body = wrap_untrusted(
        "SUBTASK-RESULTS", f"{result_text}{file_context}"
    )
    messages = [
        {"role": "system", "content": build_summarizer_prompt(output_format)},
        {"role": "user", "content": f"Original task: {task}\n\nSubtask results:\n{untrusted_body}"},
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
