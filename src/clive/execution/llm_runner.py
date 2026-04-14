"""LLM-native subtask execution — the model is the tool.

For tasks where generation IS the work: translate, summarize, rewrite,
extract structured info, classify, answer from content, explain. No pane,
no shell — a single LLM call reads input files from session_dir (and any
absolute paths referenced in the task description), produces the result
text, and writes it to ``{session_dir}/llm_{subtask.id}.txt``.

Fits into the DAG identically to other modes: upstream ``script`` produces
files → this runner consumes them via dep_context + session_dir scan →
downstream subtasks see the output file through the existing file registry.
"""

import logging
import os
import re

from llm import MODEL, chat, get_client
from models import PaneInfo, Subtask, SubtaskResult, SubtaskStatus
from prompts import build_llm_prompt
from runtime import _cancel_event, _emit, write_file

log = logging.getLogger(__name__)


_ABS_PATH_RE = re.compile(r"(/[^\s'\"]+|~/[^\s'\"]+)")
_MAX_INPUT_CHARS = 200_000


def _max_output_tokens() -> int:
    """Output cap, env-overridable so long transcripts aren't silently truncated."""
    try:
        return max(1024, int(os.environ.get("CLIVE_LLM_OUTPUT_TOKENS", "16384")))
    except ValueError:
        return 16384


def _looks_like_text(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    if not head:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _read_text(path: str, remaining: int) -> str | None:
    if remaining <= 0:
        return None
    try:
        with open(path, "r", errors="replace") as fh:
            content = fh.read(remaining + 1)
    except OSError:
        return None
    if len(content) > remaining:
        return content[:remaining] + "\n...[truncated]"
    return content


def _collect_session_inputs(session_dir: str, exclude: set[str]) -> list[tuple[str, str]]:
    """Return (name, content) for user-created text files in session_dir."""
    if not os.path.isdir(session_dir):
        return []
    candidates = []
    for fname in os.listdir(session_dir):
        if fname.startswith("_") or fname.startswith("."):
            continue
        p = os.path.join(session_dir, fname)
        if not os.path.isfile(p) or os.path.realpath(p) in exclude:
            continue
        if not _looks_like_text(p):
            log.debug("llm_runner: skipping non-text session file %s", p)
            continue
        candidates.append(p)
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    out = []
    remaining = _MAX_INPUT_CHARS
    for p in candidates:
        content = _read_text(p, remaining)
        if content is None:
            continue
        out.append((os.path.basename(p), content))
        remaining -= len(content)
        if remaining <= 0:
            break
    return out


def _collect_referenced_paths(description: str, already: set[str]) -> list[tuple[str, str]]:
    """Extract absolute/home paths from the task description and read them.

    ``already`` is a set of realpaths that have already been pulled in from
    elsewhere (typically the prospective output path); skip matches against it
    so the runner doesn't feed the model its own output.
    """
    out = []
    remaining = _MAX_INPUT_CHARS
    for match in _ABS_PATH_RE.findall(description):
        path = os.path.realpath(os.path.expanduser(match))
        if path in already:
            continue
        if not os.path.isfile(path):
            log.debug("llm_runner: referenced path does not exist: %s", match)
            continue
        if not _looks_like_text(path):
            log.debug("llm_runner: referenced path is not plain text: %s", path)
            continue
        content = _read_text(path, remaining)
        if content is None:
            continue
        already.add(path)
        out.append((path, content))
        remaining -= len(content)
        if remaining <= 0:
            break
    return out


def _format_inputs_block(inputs: list[tuple[str, str]]) -> str:
    if not inputs:
        return "(no input files found — work only from the task description)"
    parts = []
    for name, content in inputs:
        parts.append(f"--- FILE: {name} ---\n{content}")
    return "\n\n".join(parts)


_DONE_RE = re.compile(r"\n?---+\s*\n?DONE:\s*(.+?)\s*$", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n(.*)\n```$", re.DOTALL)


def _split_output_and_summary(reply: str) -> tuple[str, str]:
    """Strip an optional trailing ``--- / DONE: ...`` footer and wrapping fence."""
    text = reply.strip()
    summary = ""
    m = _DONE_RE.search(text)
    if m:
        summary = m.group(1).strip().splitlines()[0][:200]
        text = text[: m.start()].rstrip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    return text, summary


def run_subtask_llm(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute an LLM-native transformation subtask.

    Gathers input from (1) files in session_dir, (2) absolute paths referenced
    in the task description. Makes one LLM call. Writes the generated text to
    ``llm_{subtask.id}.txt`` in session_dir. No tmux interaction.
    """
    log.info(f"Subtask {subtask.id}: llm mode (model-as-tool)")

    output_path = os.path.join(session_dir, f"llm_{subtask.id}.txt")
    output_real = os.path.realpath(output_path)
    os.makedirs(session_dir, exist_ok=True)

    # Gather inputs. referenced/session paths are both normalised via realpath
    # so symlinks can't smuggle a file in twice, and neither set ever includes
    # the file we're about to write.
    referenced = _collect_referenced_paths(subtask.description, {output_real})
    referenced_reals = {os.path.realpath(name) for name, _ in referenced}
    session_inputs = _collect_session_inputs(
        session_dir,
        exclude={output_real} | referenced_reals,
    )
    inputs = referenced + session_inputs

    system_prompt = build_llm_prompt(
        subtask_description=subtask.description,
        dependency_context=dep_context,
        output_path=output_path,
    )
    user_prompt = (
        f"Task: {subtask.description}\n\n"
        f"Inputs:\n{_format_inputs_block(inputs)}\n\n"
        "Produce the result now."
    )

    if _cancel_event.is_set():
        return SubtaskResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="Cancelled before LLM call",
            output_snippet="",
            turns_used=0,
        )

    client = get_client()
    effective_model = pane_info.agent_model or MODEL
    try:
        reply, pt, ct = chat(
            client,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=_max_output_tokens(),
            model=effective_model,
        )
    except Exception as e:
        log.exception("llm-mode chat failed")
        return SubtaskResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary=f"LLM call failed: {e}",
            output_snippet="",
            turns_used=1,
            error=str(e),
        )

    _emit(on_event, "turn", subtask.id, 1, "llm generation")
    _emit(on_event, "tokens", subtask.id, pt, ct)

    output_text, model_summary = _split_output_and_summary(reply)
    if not output_text.strip():
        return SubtaskResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="LLM produced empty output",
            output_snippet=reply[:500],
            turns_used=1,
            prompt_tokens=pt,
            completion_tokens=ct,
        )

    try:
        write_file(output_path, output_text)
    except OSError as e:
        log.exception("llm-mode write_file failed")
        return SubtaskResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary=f"Failed to write output: {e}",
            output_snippet=output_text[:500],
            turns_used=1,
            prompt_tokens=pt,
            completion_tokens=ct,
            error=str(e),
        )

    # Summary is a single semantic line: it flows into dep_context for
    # downstream subtasks and into the summarizer's input. The full content
    # is already in the output file (surfaced via read_output_files) and the
    # output_snippet below; duplicating a preview here just adds noise.
    summary = model_summary or f"Wrote {os.path.basename(output_path)} ({len(output_text)} chars)"

    return SubtaskResult(
        subtask_id=subtask.id,
        status=SubtaskStatus.COMPLETED,
        summary=summary,
        output_snippet=output_text[-500:] if len(output_text) > 500 else output_text,
        turns_used=1,
        prompt_tokens=pt,
        completion_tokens=ct,
    )
