"""Auto-explore unknown tools at worker-context build time (Phase 4.2).

When a planner emits ``subtask.tools=["ripgrep"]`` but the registry has
no Tier-2 card for ``"ripgrep"``, and ``CLIVE_AUTO_EXPLORE=1`` is set,
``auto_explore_unknown_tool`` queues a background exploration. The
generated driver lands in ``drivers/.unreviewed/<tool>.md`` per the
gh#41 quarantine — the current subtask runs without it (``load_driver``
bypasses the unreviewed subdir); operator must run
``clive --promote-driver <tool>`` to activate the draft for future
sessions.

Design notes:
  - Strict opt-in. Only ``CLIVE_AUTO_EXPLORE=1`` (literal "1") enables.
    Anything else — "true", "yes", "on", unset, empty — is off. This
    avoids accidental enablement by operators who think it's a normal
    boolean flag.
  - Fire-and-forget. Exploration runs ~30s+ of LLM + tmux work; doing
    it synchronously on the prompt-construction path would block every
    worker startup. The exploration thread is a daemon — it dies with
    the process if clive exits before it completes.
  - Process-local dedup. ``_attempted_explorations`` prevents re-firing
    on the same tool name within a single clive process. Cross-process
    dedup is the operator's responsibility (multiple parallel ``clive``
    invocations on the same machine could in principle race; the
    exploration pane and ``.unreviewed/`` write are themselves
    serialized via the existing gh#41 atomic-write defenses).
  - Best-effort. Exploration failures are logged and swallowed — the
    side-effect is on the operator's behalf, not on the critical path
    of the running subtask.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)


_attempted_explorations: set[str] = set()
_attempted_lock = threading.Lock()


def is_auto_explore_enabled() -> bool:
    """Auto-explore is opt-in via ``CLIVE_AUTO_EXPLORE=1`` (literal '1')."""
    return os.environ.get("CLIVE_AUTO_EXPLORE") == "1"


# Re-exported at module scope so tests can patch ``discovery.auto.explore_tool``
# (and the two generator entry points) without reaching into submodules. The
# import is deferred to avoid pulling discovery's transitive deps when the
# auto-explore feature is unused.
def _lazy_import_discovery():
    from .explorer import explore_tool
    from .generator import generate_driver, write_generated_driver
    return explore_tool, generate_driver, write_generated_driver


# These are bound at module-import time but overridable via monkeypatch in
# tests. They're thin trampolines so ``patch.object(auto, "explore_tool", ...)``
# applies to the in-thread call path.
def explore_tool(*args, **kwargs):  # noqa: D401 — trampoline
    fn, _, _ = _lazy_import_discovery()
    return fn(*args, **kwargs)


def generate_driver(*args, **kwargs):  # noqa: D401 — trampoline
    _, fn, _ = _lazy_import_discovery()
    return fn(*args, **kwargs)


def write_generated_driver(*args, **kwargs):  # noqa: D401 — trampoline
    _, _, fn = _lazy_import_discovery()
    return fn(*args, **kwargs)


def _first_nonempty_line(text: str) -> str:
    """First non-empty stripped line of ``text`` (the driver synopsis), or ''."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _derive_memo_fields(tool_name: str, result, driver_text: str) -> tuple[str, str]:
    """Derive ``(invocation, usage)`` for the learned-tool memo.

    ``invocation`` is the command of the first successful (``exit_code == 0``)
    probe observed during exploration, or ``""`` when no probe succeeded. An
    empty invocation is the "nothing learned" signal: ``record_tool_memo`` skips
    the write so a failed re-exploration cannot clobber a previously-learned
    known-good memo with a bare-name fallback (gh#41). ``usage`` is always the
    driver's synopsis (its first non-empty stripped line).
    """
    invocation = ""
    for probe in getattr(result, "probes", None) or []:
        if getattr(probe, "success", False):
            cmd = (getattr(probe, "command", "") or "").strip()
            if cmd:
                invocation = cmd
                break
    return invocation, _first_nonempty_line(driver_text)


def _explore_async(tool_name: str, drivers_dir: Optional[str]) -> None:
    """Background exploration — fire-and-forget. Logs + swallows any failure."""
    try:
        log.info(
            "[auto-explore] exploring %r (CLIVE_AUTO_EXPLORE=1) ...", tool_name
        )
        result = explore_tool(tool_name)
        driver_text = generate_driver(tool_name, result)
        path = write_generated_driver(tool_name, driver_text, drivers_dir=drivers_dir)
        log.info(
            "[auto-explore] wrote draft for %r to %s — promote with "
            "`clive --promote-driver %s`",
            tool_name, path, tool_name,
        )
        # Persist what we learned so the next run's Tier-2 card can reuse the
        # known-good invocation (gh#41 slice 2/2). Best-effort: record_tool_memo
        # never raises, and the whole block is inside the auto-explore try/except
        # so any derivation slip is logged + swallowed like the rest. When no
        # probe succeeded, _derive_memo_fields yields an empty invocation and
        # record_tool_memo no-ops — a failed re-exploration must not clobber a
        # previously-learned good memo with a bare-name fallback (gh#41).
        from .tool_memo import record_tool_memo
        invocation, usage = _derive_memo_fields(tool_name, result, driver_text)
        record_tool_memo(tool_name, invocation, usage)
    except Exception as exc:
        log.warning("[auto-explore] failed for %r: %s", tool_name, exc)


def auto_explore_unknown_tool(
    tool_name: str,
    drivers_dir: Optional[str] = None,
) -> bool:
    """Queue a background exploration of ``tool_name``.

    Returns:
        True if a thread was queued. False if the feature is disabled or
        the tool was already attempted in this process.
    """
    if not is_auto_explore_enabled():
        return False
    with _attempted_lock:
        if tool_name in _attempted_explorations:
            return False
        _attempted_explorations.add(tool_name)

    t = threading.Thread(
        target=_explore_async,
        args=(tool_name, drivers_dir),
        daemon=True,
        name=f"auto-explore-{tool_name}",
    )
    t.start()
    log.info("[auto-explore] queued exploration of %r", tool_name)
    return True
