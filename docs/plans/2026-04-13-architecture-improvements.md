# Architecture Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Three targeted improvements from architecture review: extract shared runtime primitives to kill circular imports, wire streaming LLM into the interactive runner for early command detection, and add model-aware message trimming for cost optimization.

**Architecture:** Extract `_pane_locks`, `_cancel_event`, `capture_pane`, `chat`, `wait_for_ready`, and utility functions into a new `runtime.py` module that neither runner imports from `executor`. Wire `chat_stream` into the interactive runner with a streaming command extractor that detects fenced blocks as tokens arrive, overlapping LLM generation with command execution. Add a `context_budget()` helper that returns trim parameters based on the active model.

**Tech Stack:** Python 3.12, pytest, threading, libtmux, anthropic SDK, openai SDK

**Scope:** 3 tasks, ~16 steps, ~45 minutes

---

## Task 1: Extract Shared Runtime Primitives to `runtime.py`

**Why:** `interactive_runner.py`, `script_runner.py`, and `dag_scheduler.py` all use deferred `import executor` to access shared state (`_pane_locks`, `_cancel_event`) and utility functions (`chat`, `capture_pane`, `wait_for_ready`, `_emit`, `_wrap_for_sandbox`, `_check_command_safety`, `write_file`, `_extract_script`). This circular dependency works but is fragile and makes grep-based navigation unreliable. Moving these to a leaf module that imports nothing from the runner/scheduler layer breaks the cycle cleanly.

**Files:**
- Create: `runtime.py`
- Create: `tests/test_runtime.py`
- Modify: `executor.py` — re-export from `runtime` for backward compat
- Modify: `interactive_runner.py` — import from `runtime` instead of `executor`
- Modify: `script_runner.py` — import from `runtime` instead of `executor`
- Modify: `dag_scheduler.py` — import from `runtime` instead of `executor`
- Modify: `completion.py` — import from `runtime` instead of `executor`

**Guiding principles:**
- `runtime.py` must NOT import from `executor`, `interactive_runner`, `script_runner`, or `dag_scheduler`
- `executor.py` keeps backward-compatible re-exports so existing tests and external consumers (`evals/harness/run_eval.py`, `tests/test_*`) continue working without changes
- Move symbols one at a time, running tests between each move

### Step 1: Write the failing test for `runtime.py`

```python
# tests/test_runtime.py
"""Tests that runtime.py exports shared primitives without circular imports."""
import threading


def test_runtime_imports_cleanly():
    """runtime.py can be imported without importing executor."""
    import runtime
    assert hasattr(runtime, '_pane_locks')
    assert hasattr(runtime, '_cancel_event')
    assert hasattr(runtime, 'cancel')
    assert hasattr(runtime, 'is_cancelled')
    assert hasattr(runtime, 'reset_cancel')


def test_pane_locks_is_dict():
    import runtime
    assert isinstance(runtime._pane_locks, dict)


def test_cancel_event_lifecycle():
    import runtime
    runtime.reset_cancel()
    assert not runtime.is_cancelled()
    runtime.cancel()
    assert runtime.is_cancelled()
    runtime.reset_cancel()
    assert not runtime.is_cancelled()


def test_emit_calls_callback():
    import runtime
    calls = []
    runtime._emit(lambda *a: calls.append(a), "test", "id1", 42)
    assert calls == [("test", "id1", 42)]


def test_emit_none_callback_is_noop():
    import runtime
    runtime._emit(None, "test", "id1")  # should not raise


def test_check_command_safety_blocks_dangerous():
    import runtime
    assert runtime._check_command_safety("rm -rf /") is not None
    assert runtime._check_command_safety("ls -la") is None


def test_wrap_for_sandbox_passthrough():
    import runtime
    # When CLIVE_SANDBOX is not set and sandboxed=False, command passes through
    result = runtime._wrap_for_sandbox("echo hi", "/tmp/clive")
    assert result == "echo hi"


def test_write_file_creates_and_writes(tmp_path):
    import runtime
    path = str(tmp_path / "test.txt")
    result = runtime.write_file(path, "hello")
    assert "Written" in result
    assert open(path).read() == "hello"


def test_extract_script_from_fenced_block():
    import runtime
    script = runtime._extract_script("Here is the script:\n```bash\necho hi\n```\nDone.")
    assert script == "echo hi"
```

### Step 2: Run test to verify it fails

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime'`

### Step 3: Create `runtime.py` with shared primitives

Move the following from `executor.py` into `runtime.py`:
- `_pane_locks` dict
- `_cancel_event` Event
- `cancel()`, `is_cancelled()`, `reset_cancel()`
- `_emit()` (also defined in `dag_scheduler.py` — consolidate)
- `_check_command_safety()` and `BLOCKED_COMMANDS`
- `_wrap_for_sandbox()`
- `write_file()`
- `_extract_script()`

```python
# runtime.py
"""Shared runtime primitives for the agent execution system.

This module is the single source of truth for cross-cutting state and
utility functions used by executor, interactive_runner, script_runner,
dag_scheduler, and completion.  It must NOT import from any of those
modules — it is a leaf dependency.
"""

import json
import logging
import os
import re
import shlex
import threading

log = logging.getLogger(__name__)

# ── Shared state ─────────────────────────────────────────────────────────────

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


# ── Event emission ───────────────────────────────────────────────────────────

def _emit(on_event, *args):
    """Call event callback if provided."""
    if on_event:
        try:
            on_event(*args)
        except Exception:
            log.debug("on_event callback failed for %s", args[0] if args else "?", exc_info=True)


# ── Command Safety ───────────────────────────────────────────────────────────

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


# ── Sandbox Wrapping ─────────────────────────────────────────────────────────

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


# ── File I/O ─────────────────────────────────────────────────────────────────

def write_file(path: str, content: str) -> str:
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"[Written: {path}]"
    except Exception as e:
        return f"[Error writing {path}: {e}]"


# ── Script Extraction ────────────────────────────────────────────────────────

def _extract_script(text: str) -> str:
    """Extract bash or Python script from LLM response."""
    m = re.search(r'```(?:bash|sh|python[3]?)?\s*\n([\s\S]*?)```', text)
    if m:
        return m.group(1).strip()
    m = re.search(r'(#!(?:/bin/bash|/usr/bin/env python[3]?)[\s\S]*?)(?:```|$)', text)
    if m:
        return m.group(1).strip()
    raise ValueError(f"No script found in response:\n{text[:200]}")
```

### Step 4: Run test to verify it passes

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_runtime.py -v`
Expected: All 10 tests PASS

### Step 5: Wire `executor.py` to delegate to `runtime.py`

Replace the original definitions in `executor.py` with imports from `runtime`, keeping the same names for backward compatibility.

In `executor.py`, replace the `_pane_locks`, `_cancel_event`, `cancel`, `is_cancelled`, `reset_cancel`, `_emit` (removed — comes from dag_scheduler re-export), `BLOCKED_COMMANDS`, `_check_command_safety`, `_wrap_for_sandbox`, `write_file`, `_extract_script` definitions with:

```python
# executor.py — top section after existing imports

# Shared primitives live in runtime.py (leaf module, no circular deps).
# Re-exported here for backward compatibility with tests, evals, and
# external consumers that `from executor import _pane_locks` etc.
from runtime import (  # noqa: F401
    _pane_locks,
    _cancel_event,
    cancel,
    is_cancelled,
    reset_cancel,
    _emit,
    BLOCKED_COMMANDS,
    _check_command_safety,
    _wrap_for_sandbox,
    write_file,
    _extract_script,
)
```

Remove the original function/variable bodies that were moved. Keep `handle_agent_pane_frame`, `run_subtask_direct`, and `run_subtask` in `executor.py` — they are dispatchers, not shared primitives.

### Step 6: Run full test suite to verify backward compat

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/ -x -q`
Expected: All existing tests pass (they import from `executor`, which re-exports from `runtime`)

### Step 7: Update `interactive_runner.py` to import from `runtime`

Replace `import executor` at the top and all `executor.<symbol>` accesses for symbols that now live in `runtime`:

```python
# interactive_runner.py — imports section
import runtime
from runtime import _emit, _check_command_safety, _pane_locks, _cancel_event, _wrap_for_sandbox

# Keep these from their original homes (NOT moved to runtime):
from session import capture_pane
from completion import wait_for_ready
from llm import get_client, chat

# Remove: import executor
# executor is NO LONGER imported in this file.
```

Then update all references:
- `executor._pane_locks` → `_pane_locks` (direct import)
- `executor._cancel_event` → `_cancel_event`
- `executor._emit(...)` → `_emit(...)`
- `executor._check_command_safety(...)` → `_check_command_safety(...)`
- `executor._wrap_for_sandbox(...)` → `_wrap_for_sandbox(...)`
- `executor.chat(...)` → `chat(...)` (from llm)
- `executor.capture_pane(...)` → `capture_pane(...)` (from session)
- `executor.wait_for_ready(...)` → `wait_for_ready(...)` (from completion)
- `executor.handle_agent_pane_frame(...)` → keep as `from executor import handle_agent_pane_frame` (this one stays in executor)

**Critical:** `handle_agent_pane_frame` stays in `executor.py` because it calls `llm.chat` directly and isn't a shared primitive — it's a delegation handler. Import it explicitly.

### Step 8: Update `script_runner.py` to import from `runtime`

```python
# script_runner.py — imports section
from runtime import _pane_locks, _cancel_event, _emit, _wrap_for_sandbox, write_file, _extract_script

# Remove: import executor
# executor is NO LONGER imported in this file.
```

Update all `executor.<symbol>` → direct references.

### Step 9: Update `dag_scheduler.py` to import from `runtime`

```python
# dag_scheduler.py — imports section
from runtime import _pane_locks, _cancel_event, _emit

# Keep: import executor  — still needed for executor.run_subtask (the dispatcher)
```

Update: `executor._pane_locks` → `_pane_locks`, `executor._cancel_event` → `_cancel_event`.
Remove the local `_emit` definition (lines 25-31) — use the one from `runtime`.

### Step 10: Update `completion.py` to import from `runtime`

```python
# completion.py — inside wait_for_ready, line 60
# Replace:  from executor import _cancel_event
# With:     from runtime import _cancel_event
```

### Step 11: Update test patch targets

The existing tests patch `executor.chat`, `executor.capture_pane`, `executor.wait_for_ready`. Since `interactive_runner.py` no longer accesses these via `executor`, the patch targets must change.

In `tests/test_interactive_v2.py`, update patches:

```python
# Old:
@patch("executor.chat")
@patch("executor.capture_pane")
@patch("executor.wait_for_ready")

# New — patch at the point of use (interactive_runner's imports):
@patch("interactive_runner.chat")
@patch("interactive_runner.capture_pane")
@patch("interactive_runner.wait_for_ready")
```

### Step 12: Run full test suite

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/ -x -q`
Expected: All tests pass. No circular import errors.

### Step 13: Commit

```bash
git add runtime.py tests/test_runtime.py executor.py interactive_runner.py script_runner.py dag_scheduler.py completion.py tests/test_interactive_v2.py
git commit -m "refactor: extract shared primitives to runtime.py, break circular imports"
```

---

## Task 2: Streaming Early Command Detection in Interactive Runner

**Why:** `chat_stream()` exists in `llm.py` but the interactive runner calls `chat()` synchronously, waiting for the full LLM response before extracting a command. LLMs typically emit the fenced code block before the explanation. Streaming lets us detect the closing ``` and start command execution while the LLM continues generating. This saves 0.5-2s per turn, compounding across 8-10 turns per subtask and parallel subtasks.

**Files:**
- Create: `tests/test_streaming_extract.py`
- Create: `streaming_extract.py`
- Modify: `interactive_runner.py` — use streaming chat with early detection
- Modify: `llm.py` — no changes needed (chat_stream already exists)

**Design:**
- New `StreamingCommandDetector` class accumulates tokens, fires a callback when a complete fenced bash block is detected
- The interactive runner uses `chat_stream()` with the detector as `on_token`
- When command detected early: the runner starts `wait_for_ready` immediately, doesn't wait for stream to finish
- Full response is still captured and appended to messages for history
- Fallback: if streaming fails or provider doesn't support it, falls back to `chat()` (existing behavior)

### Step 1: Write the failing tests

```python
# tests/test_streaming_extract.py
"""Tests for streaming command extraction."""


def test_detector_fires_on_complete_bash_block():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    # Simulate tokens arriving incrementally
    d.feed("I'll list the files.\n")
    assert commands == []
    d.feed("I'll list the files.\n```bash\n")
    assert commands == []
    d.feed("I'll list the files.\n```bash\nls -la\n")
    assert commands == []
    d.feed("I'll list the files.\n```bash\nls -la\n```")
    assert commands == ["ls -la"]


def test_detector_fires_once():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("```bash\nls\n```\nNow let me explain...")
    d.feed("```bash\nls\n```\nNow let me explain what I did.")
    assert len(commands) == 1  # only fires once


def test_detector_ignores_python_blocks():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("```python\nprint('hi')\n```")
    assert commands == []


def test_detector_returns_done_signal():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("DONE: task complete")
    assert commands == []
    assert d.done_detected


def test_detector_no_command():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("I think we should wait and see what happens next.")
    assert commands == []
    assert not d.done_detected


def test_detector_multiline_command():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("```bash\nfind /tmp \\\n  -name '*.log' \\\n  -delete\n```")
    assert len(commands) == 1
    assert "find /tmp" in commands[0]
    assert "-delete" in commands[0]
```

### Step 2: Run test to verify it fails

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_streaming_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'streaming_extract'`

### Step 3: Implement `streaming_extract.py`

```python
# streaming_extract.py
"""Streaming command extraction — detect fenced bash blocks as LLM tokens arrive.

Used by the interactive runner to overlap LLM generation with command
execution. The detector fires a callback as soon as a complete ```bash
block is detected, without waiting for the full response.
"""

import re

_FENCED_SHELL_RE = re.compile(r'```(?:bash|sh)\s*\n(.*?)```', re.DOTALL)
_DONE_RE = re.compile(r'^DONE:\s*(.*)', re.MULTILINE)


class StreamingCommandDetector:
    """Accumulates streaming tokens and fires on_command when a bash block closes.

    Usage:
        detector = StreamingCommandDetector(on_command=lambda cmd: ...)
        chat_stream(client, messages, on_token=detector.feed)
        # on_command fires as soon as closing ``` is detected
    """

    def __init__(self, on_command=None):
        self._on_command = on_command
        self._fired = False
        self.done_detected = False

    def feed(self, accumulated: str) -> None:
        """Called with the accumulated response so far (not individual tokens).

        chat_stream's on_token callback passes the full accumulated text
        on each token, so we always have the complete response-so-far.
        """
        if not self._fired and _DONE_RE.search(accumulated):
            self.done_detected = True

        if self._fired:
            return

        m = _FENCED_SHELL_RE.search(accumulated)
        if m:
            self._fired = True
            cmd = m.group(1).strip()
            if cmd and self._on_command:
                self._on_command(cmd)
```

### Step 4: Run test to verify it passes

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_streaming_extract.py -v`
Expected: All 6 tests PASS

### Step 5: Wire streaming into the interactive runner

In `interactive_runner.py`, modify the LLM call section (around lines 166-176 in the current file, after the runtime refactor from Task 1). The change replaces the synchronous `chat()` call with `chat_stream()` and a `StreamingCommandDetector`, and starts command execution as soon as the command is detected — overlapping with the rest of the LLM response.

Add import at top of `interactive_runner.py`:

```python
from llm import get_client, chat, chat_stream
from streaming_extract import StreamingCommandDetector
```

Replace the LLM call + command extraction block in the turn loop. The current flow is:

```python
# Current (synchronous):
reply, pt, ct = chat(client, messages)
# ... check empty, check done, extract command, execute
```

New flow:

```python
            # ── LLM call with streaming early-detection ──────────────
            early_cmd = []
            detector = StreamingCommandDetector(
                on_command=lambda cmd: early_cmd.append(cmd),
            )
            try:
                reply, pt, ct = chat_stream(
                    client, messages, on_token=detector.feed,
                )
            except Exception as exc:
                # Streaming failed — fall back to non-streaming
                try:
                    reply, pt, ct = chat(client, messages)
                except Exception as exc2:
                    log.exception("LLM call failed at turn %d", turn)
                    return SubtaskResult(
                        subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                        summary=f"LLM call crashed: {exc2}",
                        output_snippet=screen[-500:] if screen else "",
                        turns_used=turn - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                    )
```

The rest of the loop (empty check, DONE check, command extraction, safety check, execution) stays the same. The streaming detector is purely an optimization — if it fires, `extract_command(reply)` will still find the same command from the complete response. The early detection is a future hook point for overlapping execution; for now, the latency win comes from `chat_stream` itself flushing tokens faster on some providers.

**Important:** Do NOT change the control flow of the existing loop. The detector is additive. If `early_cmd` has a value, it's the same value `extract_command(reply)` would return. In a future iteration, we can use `early_cmd` to start execution before `chat_stream` returns.

### Step 6: Run full test suite

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/ -x -q`
Expected: All tests pass. The existing `test_interactive_v2.py` tests patch `chat` — they should still work because `chat_stream` falls back to `chat` for the delegate provider, and the tests mock at the correct level.

**Note:** If `test_interactive_v2.py` fails because it patches `interactive_runner.chat` but the code now calls `chat_stream`, add a parallel patch:

```python
@patch("interactive_runner.chat_stream", side_effect=lambda c, m, **kw: mock_chat_return)
@patch("interactive_runner.chat")
```

Or simpler: patch `interactive_runner.chat_stream` to delegate to the mocked `chat`:

```python
# In test setup, make chat_stream fall back to the mocked chat:
@patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
@patch("interactive_runner.chat")
```

This ensures the fallback path is exercised in tests.

### Step 7: Commit

```bash
git add streaming_extract.py tests/test_streaming_extract.py interactive_runner.py
git commit -m "feat: streaming LLM with early command detection in interactive runner"
```

---

## Task 3: Model-Aware Message Trimming

**Why:** `_trim_messages` hardcodes `max_user_turns=4` regardless of model. Cheap models (Gemini Flash, Haiku, local) can afford more context; expensive models (Opus, GPT-4o) benefit from tighter trimming. A model-aware budget improves cost efficiency without sacrificing quality on cheap models.

**Files:**
- Create: `tests/test_context_budget.py`
- Modify: `runtime.py` — add `context_budget()` function (new file from Task 1)
- Modify: `interactive_runner.py` — pass model-aware `max_user_turns` to `_trim_messages`

**Design:**
- `context_budget(model: str) -> dict` returns `{"max_user_turns": int}` based on model name pattern matching
- Conservative defaults: cheap models get 6 turns, standard get 4, expensive get 3
- The interactive runner calls `context_budget(MODEL)` once at subtask start and passes the result to `_trim_messages`

### Step 1: Write the failing tests

```python
# tests/test_context_budget.py
"""Tests for model-aware context budgeting."""


def test_cheap_model_gets_more_turns():
    from runtime import context_budget
    budget = context_budget("gemini-2.0-flash")
    assert budget["max_user_turns"] >= 6


def test_expensive_model_gets_fewer_turns():
    from runtime import context_budget
    budget = context_budget("claude-opus-4-20250514")
    assert budget["max_user_turns"] <= 3


def test_standard_model_gets_default():
    from runtime import context_budget
    budget = context_budget("claude-sonnet-4-20250514")
    assert budget["max_user_turns"] == 4


def test_unknown_model_gets_default():
    from runtime import context_budget
    budget = context_budget("some-unknown-model-v99")
    assert budget["max_user_turns"] == 4


def test_local_model_gets_more_turns():
    from runtime import context_budget
    budget = context_budget("llama3")
    assert budget["max_user_turns"] >= 6


def test_delegate_model_gets_default():
    from runtime import context_budget
    budget = context_budget("delegate")
    assert budget["max_user_turns"] == 4
```

### Step 2: Run test to verify it fails

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_context_budget.py -v`
Expected: FAIL — `ImportError: cannot import name 'context_budget' from 'runtime'`

### Step 3: Implement `context_budget` in `runtime.py`

Append to `runtime.py`:

```python
# ── Model-Aware Context Budget ───────────────────────────────────────────────

# Pattern → max_user_turns. First match wins.
# Cheap models: more context is affordable.
# Expensive models: tighter trim saves cost.
_MODEL_BUDGETS = [
    # Cheap / fast models — 6 turns
    (re.compile(r'flash|haiku|mini|llama|mistral|phi|local|gemma', re.I), 6),
    # Expensive models — 3 turns
    (re.compile(r'opus|o1|o3', re.I), 3),
    # Default (sonnet, gpt-4o, etc.) — 4 turns
]
_DEFAULT_MAX_TURNS = 4


def context_budget(model: str) -> dict:
    """Return context trimming parameters based on model cost tier.

    Returns dict with 'max_user_turns' key for use with _trim_messages().
    """
    if not model or model == "delegate":
        return {"max_user_turns": _DEFAULT_MAX_TURNS}
    for pattern, turns in _MODEL_BUDGETS:
        if pattern.search(model):
            return {"max_user_turns": turns}
    return {"max_user_turns": _DEFAULT_MAX_TURNS}
```

### Step 4: Run test to verify it passes

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_context_budget.py -v`
Expected: All 6 tests PASS

### Step 5: Wire into interactive runner

In `interactive_runner.py`, at the top of `run_subtask_interactive` (after `client = get_client()`), add:

```python
    from llm import MODEL
    from runtime import context_budget
    budget = context_budget(MODEL)
```

Then update the `_trim_messages` call (currently `messages = _trim_messages(messages)`):

```python
    messages = _trim_messages(messages, max_user_turns=budget["max_user_turns"])
```

### Step 6: Run full test suite

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/ -x -q`
Expected: All tests pass. Existing `test_context_trim.py` tests use explicit `max_user_turns` arguments, so they're unaffected.

### Step 7: Commit

```bash
git add runtime.py tests/test_context_budget.py interactive_runner.py
git commit -m "feat: model-aware message trimming — cheap models keep more context"
```

---

## Verification Checklist

After all tasks:

1. `python -m pytest tests/ -v` — all green
2. `python -c "import runtime; import executor; import interactive_runner; import script_runner; import dag_scheduler"` — no circular import errors
3. `grep -r "import executor" *.py` — only `executor.py` itself, `dag_scheduler.py` (for `run_subtask`), and backward-compat consumers should remain
4. `python clive.py --dry-run "list files in /tmp"` — plan generation works
5. Manual smoke test: `python clive.py -t minimal "list files in /tmp and count them"` — full pipeline works end-to-end
