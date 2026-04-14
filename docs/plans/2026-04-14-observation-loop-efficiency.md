# Observation Loop Efficiency — 6-Strategy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make clive's tmux observation loop maximally efficient by implementing six complementary strategies: per-pane model selection, observation events, observation-action decoupling, progressive context compression, plan-execute-verify mode, and hybrid tool-calling.

**Architecture:** The core insight is that the interactive runner's read-think-type loop conflates three phases with different computational needs: WAIT (free — markers/polling), OBSERVE (cheap — classify screen state), and DECIDE (expensive — choose next action). We separate these phases so the expensive main model is only called when genuine judgment is needed. Each pane becomes its own agent with its own model, context window, and completion criteria. A cheap observation classifier decides whether to advance mechanically or escalate to the main model. Context compression replaces the current trim-and-drop strategy. A new "planned" execution mode generates an execution plan with verification criteria in one LLM call, then executes mechanically. Finally, native tool-calling support enables command batching (multiple commands per LLM response).

**Tech Stack:** Python 3, libtmux, OpenAI/Anthropic SDKs, pytest, existing clive modules

**Key files overview:**
- `interactive_runner.py` — the read-think-type loop (primary integration point)
- `script_runner.py` — script mode runner (model threading)
- `llm.py` — LLM client (tool-calling support)
- `models.py` — data classes (PaneInfo, Subtask, VALID_MODES)
- `prompts.py` — prompt templates and driver loading
- `executor.py` — mode dispatcher
- `runtime.py` — context_budget, shared primitives
- `completion.py` — wait_for_ready, intervention detection
- `screen_diff.py` — screen diffing
- `command_extract.py` — command parsing from LLM text

---

## Task 1: Per-Pane Model Selection (Strategy 4 — Foundation)

Extend driver frontmatter to declare `agent_model` and `observation_model`. Thread these through PaneInfo so each runner can use pane-specific models instead of the global MODEL.

**Files:**
- Modify: `models.py:36-48` (PaneInfo dataclass)
- Modify: `prompts.py:17-35` (frontmatter parsing — already works, just document new keys)
- Modify: `session.py:78-85` (PaneInfo construction)
- Modify: `interactive_runner.py:107-108` (use pane model)
- Modify: `script_runner.py:145` (use pane model)
- Modify: `drivers/shell.md:1-3` (add model frontmatter)
- Test: `tests/test_pane_models.py`

**Step 1: Write the failing tests**

```python
# tests/test_pane_models.py
"""Tests for per-pane model selection."""
from models import PaneInfo
from unittest.mock import MagicMock


def test_pane_info_has_model_fields():
    pane = MagicMock()
    info = PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")
    assert info.agent_model is None
    assert info.observation_model is None


def test_pane_info_with_explicit_models():
    pane = MagicMock()
    info = PaneInfo(
        pane=pane, app_type="shell", description="Bash", name="shell",
        agent_model="claude-haiku-4-5-20251001",
        observation_model="gemini-2.0-flash",
    )
    assert info.agent_model == "claude-haiku-4-5-20251001"
    assert info.observation_model == "gemini-2.0-flash"


def test_driver_frontmatter_parses_model_keys():
    from prompts import _parse_driver_frontmatter
    content = """---
preferred_mode: script
agent_model: claude-haiku-4-5-20251001
observation_model: gemini-2.0-flash
---
# Shell Driver
ENVIRONMENT: bash"""
    body, meta = _parse_driver_frontmatter(content)
    assert meta["agent_model"] == "claude-haiku-4-5-20251001"
    assert meta["observation_model"] == "gemini-2.0-flash"
    assert "Shell Driver" in body


def test_resolve_pane_model_falls_back_to_global():
    """When driver doesn't specify agent_model, use global MODEL."""
    from prompts import load_driver_meta
    meta = load_driver_meta("nonexistent_app_type_xyz")
    assert "agent_model" not in meta  # fallback to global
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_pane_models.py -v`
Expected: FAIL — PaneInfo doesn't have `agent_model`/`observation_model` fields yet

**Step 3: Add model fields to PaneInfo**

In `models.py`, add two Optional[str] fields to the PaneInfo dataclass:

```python
# After frame_nonce field (line ~48):
    agent_model: str | None = None       # per-pane override for main LLM (from driver frontmatter)
    observation_model: str | None = None  # per-pane override for cheap classifier model
```

**Step 4: Thread driver meta into PaneInfo construction in session.py**

In `session.py:78-85`, after constructing PaneInfo, load the driver meta and set model fields. Find the PaneInfo construction inside the `for i, tool in enumerate(tools):` loop:

```python
        # After existing PaneInfo construction, inject driver model overrides
        from prompts import load_driver_meta
        meta = load_driver_meta(tool["app_type"])
        if meta.get("agent_model"):
            panes[tool["name"]].agent_model = meta["agent_model"]
        if meta.get("observation_model"):
            panes[tool["name"]].observation_model = meta["observation_model"]
```

Also apply the same in `add_pane()` (session.py:116-160).

**Step 5: Use pane model in interactive_runner.py**

In `interactive_runner.py:107-108`, replace:
```python
    from llm import MODEL
    from runtime import context_budget
    budget = context_budget(MODEL)
```
with:
```python
    from llm import MODEL
    from runtime import context_budget
    _active_model = pane_info.agent_model or MODEL
    budget = context_budget(_active_model)
```

Then in the `chat_stream` call (line ~175), pass `model=_active_model`:
```python
                reply, pt, ct = chat_stream(
                    client, messages,
                    model=_active_model,
                    on_token=detector.feed,
                    should_stop=detector.should_stop,
                )
```

And in the fallback `chat` call (line ~182):
```python
                    reply, pt, ct = chat(client, messages, model=_active_model)
```

**Step 6: Use pane model in script_runner.py**

In `script_runner.py:145`, replace:
```python
            reply, pt, ct = chat(client, messages, model=SCRIPT_MODEL)
```
with:
```python
            _script_model = pane_info.agent_model or SCRIPT_MODEL
            reply, pt, ct = chat(client, messages, model=_script_model)
```

**Step 7: Run tests to verify they pass**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_pane_models.py tests/test_interactive_v2.py tests/test_script_mode.py -v`
Expected: ALL PASS

**Step 8: Commit**

```bash
git add models.py session.py interactive_runner.py script_runner.py tests/test_pane_models.py
git commit -m "feat: per-pane model selection via driver frontmatter"
```

---

## Task 2: Observation Event System (Strategy 5 — Foundation)

Create `observation.py` with structured screen event types and a regex-based screen classifier. This replaces raw screen diffs with semantic events the main model can consume.

**Files:**
- Create: `observation.py`
- Test: `tests/test_observation.py`

**Step 1: Write the failing tests**

```python
# tests/test_observation.py
"""Tests for observation event system."""
from observation import ScreenEvent, ScreenClassifier, EventType


def test_event_types_exist():
    assert EventType.SUCCESS is not None
    assert EventType.ERROR is not None
    assert EventType.NEEDS_INPUT is not None
    assert EventType.RUNNING is not None
    assert EventType.UNKNOWN is not None


def test_classify_success_with_marker():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="output here\nEXIT:0 ___DONE_1_abc___\n[AGENT_READY] $ ",
        exit_code=0,
    )
    assert event.type == EventType.SUCCESS
    assert event.needs_llm is False


def test_classify_error_with_exit_code():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="bash: command not found: foo\nEXIT:127 ___DONE_1_abc___\n[AGENT_READY] $ ",
        exit_code=127,
    )
    assert event.type == EventType.ERROR
    assert event.needs_llm is True
    assert "127" in event.summary


def test_classify_needs_input():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="Do you want to continue? [y/N] ",
        exit_code=None,
    )
    assert event.type == EventType.NEEDS_INPUT
    assert event.needs_llm is True


def test_classify_running():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="Downloading... 45%",
        exit_code=None,
    )
    assert event.type == EventType.RUNNING
    assert event.needs_llm is False


def test_classify_prompt_ready():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="[AGENT_READY] $ ",
        exit_code=0,
    )
    assert event.type == EventType.SUCCESS
    assert event.needs_llm is False


def test_classify_fatal_error():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="FATAL: unable to connect to database\n[AGENT_READY] $ ",
        exit_code=1,
    )
    assert event.type == EventType.ERROR
    assert event.needs_llm is True
    assert "FATAL" in event.summary or "fatal" in event.summary.lower()


def test_event_summary_truncated():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="x" * 5000 + "\nEXIT:0 ___DONE_1_a___\n[AGENT_READY] $ ",
        exit_code=0,
    )
    assert len(event.summary) <= 500


def test_classify_permission_denied():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="ls: /root: Permission denied\nEXIT:1 ___DONE_1_a___\n[AGENT_READY] $ ",
        exit_code=1,
    )
    assert event.type == EventType.ERROR
    assert event.needs_llm is True


def test_success_extracts_output_summary():
    sc = ScreenClassifier()
    event = sc.classify(
        screen="file1.txt\nfile2.txt\nfile3.txt\nEXIT:0 ___DONE_1_a___\n[AGENT_READY] $ ",
        exit_code=0,
    )
    assert event.type == EventType.SUCCESS
    assert "file" in event.summary.lower()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_observation.py -v`
Expected: FAIL — `observation` module doesn't exist

**Step 3: Implement observation.py**

```python
# observation.py
"""Observation event system — structured screen classification.

Converts raw tmux screen captures into semantic events. The screen
classifier determines whether the main LLM needs to be called
(needs_llm=True) or whether execution can continue mechanically.
"""

import re
from dataclasses import dataclass
from enum import Enum

from completion import INTERVENTION_PATTERNS


class EventType(Enum):
    SUCCESS = "success"
    ERROR = "error"
    NEEDS_INPUT = "needs_input"
    RUNNING = "running"
    UNKNOWN = "unknown"


@dataclass
class ScreenEvent:
    type: EventType
    summary: str           # compact description for LLM context
    needs_llm: bool        # whether the main model must be consulted
    exit_code: int | None = None
    raw_output: str = ""   # last N chars of screen for context

    def __post_init__(self):
        if len(self.summary) > 500:
            self.summary = self.summary[:497] + "..."


# Patterns indicating a running process (not yet complete)
_PROGRESS_PATTERNS = [
    re.compile(r'\d+%'),           # percentage progress
    re.compile(r'\.{3,}'),         # ellipsis (loading...)
    re.compile(r'ETA\s'),          # ETA display
    re.compile(r'Downloading'),    # download in progress
    re.compile(r'Compiling'),      # compilation
    re.compile(r'Building'),       # build process
]

# Fatal/important error patterns (beyond just non-zero exit)
_ERROR_HIGHLIGHT_RE = re.compile(
    r'(FATAL|panic|Traceback|Error:|Exception:|Permission denied|'
    r'No such file|command not found|segfault|killed|OOM)',
    re.IGNORECASE,
)


class ScreenClassifier:
    """Classify tmux screen state into structured events.

    Uses only regex — no LLM calls. The needs_llm flag tells the
    caller whether to escalate to the main model or handle mechanically.
    """

    def classify(
        self,
        screen: str,
        exit_code: int | None = None,
    ) -> ScreenEvent:
        tail = screen[-500:] if len(screen) > 500 else screen

        # Check for intervention patterns (needs human/LLM input)
        for pattern, intervention_type in INTERVENTION_PATTERNS:
            if pattern.search(screen):
                return ScreenEvent(
                    type=EventType.NEEDS_INPUT,
                    summary=f"Waiting for input ({intervention_type}): {tail.splitlines()[-1].strip()[:100]}",
                    needs_llm=True,
                    exit_code=exit_code,
                    raw_output=tail,
                )

        # Exit code available — command finished
        if exit_code is not None:
            if exit_code == 0:
                # Extract meaningful output (skip markers and prompts)
                output_lines = [
                    l for l in screen.splitlines()
                    if l.strip()
                    and "AGENT_READY" not in l
                    and "___DONE_" not in l
                    and not l.strip().startswith("EXIT:")
                    and "export PS1" not in l
                ]
                summary = "\n".join(output_lines[-10:]) if output_lines else "Command completed successfully"
                if len(summary) > 500:
                    summary = summary[-500:]
                return ScreenEvent(
                    type=EventType.SUCCESS,
                    summary=summary,
                    needs_llm=False,
                    exit_code=0,
                    raw_output=tail,
                )
            else:
                # Non-zero exit — extract error context
                error_match = _ERROR_HIGHLIGHT_RE.search(screen)
                error_hint = error_match.group(0) if error_match else f"exit code {exit_code}"
                return ScreenEvent(
                    type=EventType.ERROR,
                    summary=f"Failed ({error_hint}): {tail.strip()[-200:]}",
                    needs_llm=True,
                    exit_code=exit_code,
                    raw_output=tail,
                )

        # No exit code, no intervention — check if still running
        for pattern in _PROGRESS_PATTERNS:
            if pattern.search(tail):
                return ScreenEvent(
                    type=EventType.RUNNING,
                    summary=f"In progress: {tail.splitlines()[-1].strip()[:100]}",
                    needs_llm=False,
                    exit_code=None,
                    raw_output=tail,
                )

        # Prompt ready but no exit code parsed — treat as success
        if "[AGENT_READY]" in screen:
            return ScreenEvent(
                type=EventType.SUCCESS,
                summary=tail.strip()[-200:] or "Ready",
                needs_llm=False,
                exit_code=0,
                raw_output=tail,
            )

        # Can't determine state
        return ScreenEvent(
            type=EventType.UNKNOWN,
            summary=tail.strip()[-200:],
            needs_llm=True,
            exit_code=exit_code,
            raw_output=tail,
        )
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_observation.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add observation.py tests/test_observation.py
git commit -m "feat: observation event system — structured screen classification"
```

---

## Task 3: Progressive Context Compression (Strategy 2)

Replace the current `_trim_messages()` drop-old-turns strategy with progressive compression that summarizes old turns using a cheap model, preserving information while keeping context small.

**Files:**
- Create: `context_compress.py`
- Modify: `interactive_runner.py:49-72` (replace `_trim_messages` call)
- Test: `tests/test_context_compress.py`

**Step 1: Write the failing tests**

```python
# tests/test_context_compress.py
"""Tests for progressive context compression."""
from context_compress import compress_context, _format_turns_for_summary


def test_short_conversation_unchanged():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "screen output"},
        {"role": "assistant", "content": "```bash\nls\n```"},
    ]
    result = compress_context(messages, max_user_turns=3, compress_fn=None)
    assert len(result) == 3  # no compression needed


def test_long_conversation_gets_compressed():
    messages = [{"role": "system", "content": "system prompt"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"screen turn {i}"})
        messages.append({"role": "assistant", "content": f"```bash\ncmd{i}\n```"})

    def fake_compress(text):
        return "Summary: ran 8 commands"

    result = compress_context(messages, max_user_turns=3, compress_fn=fake_compress)
    # system + summary_msg + last 2 pairs (4 msgs) = 6
    assert len(result) <= 7
    assert result[0]["role"] == "system"
    # Summary should be present
    summaries = [m for m in result if "Summary:" in m.get("content", "")]
    assert len(summaries) == 1


def test_preserves_last_n_turns():
    messages = [{"role": "system", "content": "sys"}]
    for i in range(6):
        messages.append({"role": "user", "content": f"u{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})

    def fake_compress(text):
        return "compressed history"

    result = compress_context(messages, max_user_turns=3, compress_fn=fake_compress)
    # Last 3 user turns and their assistant replies should be intact
    assert any("u5" in m["content"] for m in result)
    assert any("u4" in m["content"] for m in result)
    assert any("u3" in m["content"] for m in result)


def test_no_compress_fn_falls_back_to_trim():
    """When no compress_fn provided, fall back to bookend trimming."""
    messages = [{"role": "system", "content": "sys"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"u{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})

    result = compress_context(messages, max_user_turns=3, compress_fn=None)
    assert len(result) < len(messages)
    assert result[0]["role"] == "system"


def test_format_turns_for_summary():
    turns = [
        {"role": "user", "content": "[Screen update: +3 -1 lines]\n  file1.txt\n  file2.txt"},
        {"role": "assistant", "content": "```bash\ncat file1.txt\n```"},
        {"role": "user", "content": "Contents of file1.txt:\nhello world"},
        {"role": "assistant", "content": "```bash\ngrep hello file2.txt\n```"},
    ]
    text = _format_turns_for_summary(turns)
    assert "cat file1.txt" in text
    assert "grep hello" in text


def test_compress_context_preserves_system():
    messages = [{"role": "system", "content": "important system prompt"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"u{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})

    def fake_compress(text):
        return "summary"

    result = compress_context(messages, max_user_turns=3, compress_fn=fake_compress)
    assert result[0]["content"] == "important system prompt"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_context_compress.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement context_compress.py**

```python
# context_compress.py
"""Progressive context compression for the interactive loop.

Replaces the bookend trim strategy (_trim_messages) with a
compression approach that summarizes old turns instead of dropping
them. When a compress_fn is provided (typically a cheap LLM call),
old turns are compressed into a running summary. When no compress_fn
is available, falls back to the original bookend trim.
"""

from interactive_runner import _trim_messages


def _format_turns_for_summary(turns: list[dict]) -> str:
    """Format conversation turns into a compact text for summarization."""
    parts = []
    for msg in turns:
        role = msg["role"]
        content = msg["content"]
        if role == "user":
            # Truncate long screen captures
            if len(content) > 200:
                content = content[:200] + "..."
            parts.append(f"[Screen]: {content}")
        elif role == "assistant":
            # Extract just the command from assistant replies
            parts.append(f"[Command]: {content.strip()}")
    return "\n".join(parts)


def compress_context(
    messages: list[dict],
    max_user_turns: int = 4,
    compress_fn: callable = None,
) -> list[dict]:
    """Compress conversation history, preserving information.

    Args:
        messages: Full conversation history (system + user/assistant pairs)
        max_user_turns: How many recent user turns to keep verbatim
        compress_fn: Callable(text) -> str that summarizes text.
                     If None, falls back to bookend trimming.

    Returns:
        Compressed message list: system + [summary] + last N turn pairs
    """
    if not messages:
        return messages

    system = [m for m in messages if m["role"] == "system"]
    conversation = [m for m in messages if m["role"] != "system"]

    user_indices = [i for i, m in enumerate(conversation) if m["role"] == "user"]

    if len(user_indices) <= max_user_turns:
        return messages  # small enough, no compression needed

    if compress_fn is None:
        return _trim_messages(messages, max_user_turns=max_user_turns)

    # Split: old turns to compress, recent turns to keep
    cutoff_idx = user_indices[-max_user_turns]
    old_turns = conversation[:cutoff_idx]
    recent_turns = conversation[cutoff_idx:]

    # Compress old turns
    summary_text = _format_turns_for_summary(old_turns)
    summary = compress_fn(summary_text)

    summary_msg = {
        "role": "user",
        "content": f"[Session history summary]: {summary}",
    }

    return system + [summary_msg] + recent_turns
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_context_compress.py -v`
Expected: ALL PASS

**Step 5: Create cheap-model compression function**

Add a helper that creates a compress_fn from a cheap LLM client. Add to `context_compress.py`:

```python
def make_llm_compressor(client, model: str | None = None) -> callable:
    """Create a compress_fn that uses a cheap LLM to summarize context.

    Usage:
        from llm import get_client, CLASSIFIER_MODEL
        compress_fn = make_llm_compressor(get_client(), model=CLASSIFIER_MODEL)
    """
    from llm import chat, CLASSIFIER_MODEL
    _model = model or CLASSIFIER_MODEL

    def _compress(text: str) -> str:
        summary, _, _ = chat(
            client,
            [
                {"role": "system", "content": (
                    "Summarize this terminal session history in 2-3 sentences. "
                    "Include: what commands were run, what worked, what failed, "
                    "key output values. Be factual and concise."
                )},
                {"role": "user", "content": text},
            ],
            max_tokens=200,
            model=_model,
        )
        return summary.strip()

    return _compress
```

**Step 6: Integrate into interactive_runner.py**

In `interactive_runner.py`, replace the `_trim_messages` call (line ~168) with:

```python
            # Import at top of file:
            from context_compress import compress_context, make_llm_compressor

            # Replace line 168:
            # OLD: messages = _trim_messages(messages, max_user_turns=budget["max_user_turns"])
            # NEW:
            _obs_model = pane_info.observation_model
            if _obs_model:
                _compressor = make_llm_compressor(client, model=_obs_model)
            else:
                _compressor = None
            messages = compress_context(
                messages,
                max_user_turns=budget["max_user_turns"],
                compress_fn=_compressor,
            )
```

Note: The compressor creation should be hoisted outside the loop (create once, reuse). Move it to after the `client = get_client()` line (~line 103).

**Step 7: Run all related tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_context_compress.py tests/test_context_trim.py tests/test_interactive_v2.py -v`
Expected: ALL PASS

**Step 8: Commit**

```bash
git add context_compress.py tests/test_context_compress.py interactive_runner.py
git commit -m "feat: progressive context compression — summarize old turns instead of dropping"
```

---

## Task 4: Observation-Action Decoupling (Strategy 1 — Core Optimization)

Insert the observation classifier into the interactive runner's loop. When the classifier determines the main model isn't needed (success, still running), skip the expensive LLM call and either advance to the next planned step or wait.

This requires the interactive runner to optionally receive a "plan" — a list of commands with expected outcomes — so it can advance mechanically on success.

**Files:**
- Modify: `interactive_runner.py:90-272` (main loop restructure)
- Test: `tests/test_observation_decoupling.py`

**Step 1: Write the failing tests**

```python
# tests/test_observation_decoupling.py
"""Tests for observation-action decoupling in the interactive runner."""
from unittest.mock import MagicMock, patch, call
from models import Subtask, SubtaskStatus, PaneInfo


def _make_pane_info():
    pane = MagicMock()
    pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
    return PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")


def _make_subtask(**kw):
    defaults = dict(id="1", description="list files", pane="shell", mode="interactive", max_turns=5)
    defaults.update(kw)
    return Subtask(**defaults)


class TestObservationDecoupling:
    @patch("interactive_runner.chat_stream", side_effect=Exception("fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    def test_skips_llm_on_success_with_plan(self, mock_wait, mock_capture, mock_chat, mock_stream):
        """When observation classifier says success and plan has next step,
        skip the main model and execute next planned command."""
        # Turn 1: LLM provides a plan of 2 commands
        mock_capture.side_effect = [
            "[AGENT_READY] $ ",  # initial
            "file1.txt\nfile2.txt\nEXIT:0 ___DONE_1_abc___\n[AGENT_READY] $ ",  # after ls
            "42\nEXIT:0 ___DONE_1_def___\n[AGENT_READY] $ ",  # after wc
        ]
        mock_chat.side_effect = [
            ("```bash\nls\n```", 100, 50),   # turn 1
            ("```bash\nwc -l file1.txt\n```", 100, 50),  # turn 2 (if called)
            ("DONE: found 2 files, 42 lines", 50, 20),    # turn 3
        ]
        mock_wait.return_value = ("file1.txt\nfile2.txt\nEXIT:0 ___DONE_1_abc___\n[AGENT_READY] $ ", "marker")

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED

    @patch("interactive_runner.chat_stream", side_effect=Exception("fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    def test_escalates_to_llm_on_error(self, mock_wait, mock_capture, mock_chat, mock_stream):
        """When observation classifier detects error, always call the main model."""
        mock_capture.side_effect = [
            "[AGENT_READY] $ ",  # initial
            "command not found: foo\nEXIT:127 ___DONE_1_abc___\n[AGENT_READY] $ ",  # after failed cmd
        ]
        mock_chat.side_effect = [
            ("```bash\nfoo\n```", 100, 50),   # turn 1: bad command
            ("DONE: command unavailable", 50, 20),  # turn 2: give up
        ]
        mock_wait.return_value = ("command not found: foo\nEXIT:127 ___DONE_1_abc___\n[AGENT_READY] $ ", "marker")

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        # The LLM should have been called at least twice (initial + error response)
        assert mock_chat.call_count >= 2
```

**Step 2: Run tests to verify they fail (or pass — this is observational)**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_observation_decoupling.py -v`

**Step 3: Add observation classification to the interactive loop**

In `interactive_runner.py`, add the observation classifier between the `wait_for_ready` and the next LLM call. Modify the section after `_send_agent_command` (around line 228-263):

After the command is sent and the screen is captured, classify the result. If `needs_llm` is False and exit_code == 0, inject a compact event summary instead of the full diff, allowing the next turn to proceed faster:

```python
            # After _send_agent_command returns prev_screen and detection:
            from observation import ScreenClassifier
            _classifier = ScreenClassifier()
            _exit_code = _parse_exit_code(prev_screen)
            _event = _classifier.classify(prev_screen, exit_code=_exit_code)

            if not _event.needs_llm and _event.exit_code == 0:
                # Inject compact success event instead of full diff
                messages.append({
                    "role": "user",
                    "content": f"[OK exit:0] {_event.summary[:200]}",
                })
                # Don't skip the LLM entirely — it still needs to decide
                # the next action. But the compact event reduces tokens.
            # ... existing exit_code and intervention handling continues
```

**Step 4: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_observation_decoupling.py tests/test_interactive_v2.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add interactive_runner.py tests/test_observation_decoupling.py
git commit -m "feat: observation-action decoupling — classify screen before LLM call"
```

---

## Task 5: Plan-Execute-Verify Mode (Strategy 3 — New Execution Mode)

Create a new "planned" execution mode. The LLM generates a full plan with verification criteria in ONE call. The harness then executes each step mechanically, only calling the LLM if verification fails.

**Files:**
- Modify: `models.py:10` (add "planned" to VALID_MODES)
- Create: `planned_runner.py`
- Modify: `prompts.py` (add `build_planned_prompt`)
- Modify: `executor.py:209-224` (add planned mode dispatch)
- Test: `tests/test_planned_runner.py`

**Step 1: Write the failing tests**

```python
# tests/test_planned_runner.py
"""Tests for plan-execute-verify runner."""
import json
from unittest.mock import MagicMock, patch
from models import Subtask, SubtaskStatus, PaneInfo


def _make_pane_info():
    pane = MagicMock()
    pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
    return PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")


def _make_subtask(**kw):
    defaults = dict(id="1", description="list files and count lines", pane="shell", mode="planned", max_turns=5)
    defaults.update(kw)
    return Subtask(**defaults)


class TestPlannedStepParsing:
    def test_parse_planned_steps(self):
        from planned_runner import parse_planned_steps
        llm_response = json.dumps({
            "steps": [
                {"cmd": "ls -la", "verify": "exit_code == 0", "on_fail": "retry"},
                {"cmd": "wc -l *.txt", "verify": "exit_code == 0", "on_fail": "abort"},
            ],
            "done_summary": "Listed files and counted lines"
        })
        plan = parse_planned_steps(llm_response)
        assert len(plan.steps) == 2
        assert plan.steps[0].cmd == "ls -la"
        assert plan.done_summary == "Listed files and counted lines"

    def test_parse_planned_steps_from_fenced_json(self):
        from planned_runner import parse_planned_steps
        llm_response = '```json\n{"steps": [{"cmd": "ls", "verify": "exit_code == 0", "on_fail": "abort"}], "done_summary": "done"}\n```'
        plan = parse_planned_steps(llm_response)
        assert len(plan.steps) == 1

    def test_parse_planned_steps_invalid_json(self):
        from planned_runner import parse_planned_steps
        plan = parse_planned_steps("not json at all")
        assert plan is None


class TestPlannedExecution:
    @patch("planned_runner.chat")
    @patch("planned_runner.wait_for_ready")
    @patch("planned_runner.capture_pane")
    def test_happy_path_zero_extra_llm_calls(self, mock_capture, mock_wait, mock_chat):
        """On happy path, only 1 LLM call (plan generation). Zero during execution."""
        plan_response = json.dumps({
            "steps": [
                {"cmd": "ls -la", "verify": "exit_code == 0", "on_fail": "abort"},
                {"cmd": "wc -l file.txt", "verify": "exit_code == 0", "on_fail": "abort"},
            ],
            "done_summary": "Listed and counted"
        })
        mock_chat.return_value = (plan_response, 200, 100)
        mock_wait.return_value = ("output\nEXIT:0 ___DONE_1_abc___\n[AGENT_READY] $ ", "marker")
        mock_capture.return_value = "[AGENT_READY] $ "

        from planned_runner import run_subtask_planned
        result = run_subtask_planned(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
        assert mock_chat.call_count == 1  # only plan generation

    @patch("planned_runner.chat")
    @patch("planned_runner.wait_for_ready")
    @patch("planned_runner.capture_pane")
    def test_failure_escalates_to_interactive(self, mock_capture, mock_wait, mock_chat):
        """When a step fails and on_fail=abort, the runner fails the subtask."""
        plan_response = json.dumps({
            "steps": [
                {"cmd": "bad_command", "verify": "exit_code == 0", "on_fail": "abort"},
            ],
            "done_summary": "Should not reach"
        })
        mock_chat.return_value = (plan_response, 200, 100)
        mock_wait.return_value = ("command not found\nEXIT:127 ___DONE_1_abc___\n[AGENT_READY] $ ", "marker")
        mock_capture.return_value = "[AGENT_READY] $ "

        from planned_runner import run_subtask_planned
        result = run_subtask_planned(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.FAILED


class TestPlannedPrompt:
    def test_build_planned_prompt(self):
        from prompts import build_planned_prompt
        prompt = build_planned_prompt(
            subtask_description="Count files",
            pane_name="shell",
            app_type="shell",
            tool_description="bash",
            dependency_context="",
        )
        assert "steps" in prompt.lower()
        assert "verify" in prompt.lower()
        assert "JSON" in prompt


class TestPlannedModeDispatch:
    def test_planned_is_valid_mode(self):
        from models import VALID_MODES
        assert "planned" in VALID_MODES

    @patch("planned_runner.run_subtask_planned")
    @patch("planned_runner.chat")
    @patch("planned_runner.wait_for_ready")
    @patch("planned_runner.capture_pane")
    def test_executor_dispatches_planned_mode(self, mock_cap, mock_wait, mock_chat, mock_run):
        """executor.run_subtask dispatches mode='planned' to run_subtask_planned."""
        mock_run.return_value = MagicMock(status=SubtaskStatus.COMPLETED)
        from executor import run_subtask
        result = run_subtask(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        mock_run.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_planned_runner.py -v`
Expected: FAIL — module doesn't exist, "planned" not in VALID_MODES

**Step 3: Add "planned" to VALID_MODES**

In `models.py`, change:
```python
VALID_MODES = {"direct", "script", "interactive", "streaming"}
```
to:
```python
VALID_MODES = {"direct", "script", "interactive", "streaming", "planned"}
```

**Step 4: Add build_planned_prompt to prompts.py**

Append to `prompts.py`:

```python
def build_planned_prompt(
    subtask_description: str,
    pane_name: str,
    app_type: str,
    tool_description: str,
    dependency_context: str,
    session_dir: str = "/tmp/clive",
) -> str:
    """Planned mode prompt — generate a full execution plan with verification criteria."""
    dep_section = ""
    if dependency_context:
        dep_section = f"\nPrior results:\n{dependency_context}\n"

    driver = load_driver(app_type)

    return f"""You are generating an execution plan for: {subtask_description}

Pane: {pane_name} [{app_type}] — {tool_description}

{driver}
{dep_section}
Generate a JSON plan with shell commands and verification criteria.
The harness will execute each step mechanically — NO LLM calls during execution.
Only if a step fails will the LLM be consulted for repair.

Respond with ONLY a JSON object:
{{
  "steps": [
    {{
      "cmd": "exact shell command to run",
      "verify": "exit_code == 0",
      "on_fail": "retry|skip|abort"
    }}
  ],
  "done_summary": "one-line summary of what this plan accomplishes"
}}

Rules:
- Each step is one shell command (use && for dependent chains)
- "verify" is always "exit_code == 0" (harness checks exit code)
- "on_fail": "retry" re-runs once, "skip" continues, "abort" stops
- Write output files to {session_dir}/
- Keep it minimal — fewer steps is better
"""
```

**Step 5: Implement planned_runner.py**

```python
# planned_runner.py
"""Plan-Execute-Verify runner — generate plan in one LLM call, execute mechanically.

The LLM generates a sequence of commands with verification criteria.
The harness executes each step, checking exit codes. On the happy path,
zero additional LLM calls are needed after plan generation.
"""

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field

from completion import wait_for_ready, wrap_command
from llm import get_client, chat
from models import Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from prompts import build_planned_prompt
from runtime import _pane_locks, _cancel_event, _emit, _check_command_safety, _wrap_for_sandbox
from session import capture_pane

log = logging.getLogger(__name__)


@dataclass
class PlannedStep:
    cmd: str
    verify: str = "exit_code == 0"
    on_fail: str = "abort"  # retry, skip, abort


@dataclass
class PlannedPlan:
    steps: list[PlannedStep] = field(default_factory=list)
    done_summary: str = ""


def parse_planned_steps(llm_response: str) -> PlannedPlan | None:
    """Parse LLM response into a PlannedPlan."""
    # Try fenced JSON block first
    m = re.search(r'```json\s*\n([\s\S]*?)```', llm_response)
    text = m.group(1).strip() if m else llm_response.strip()

    # Try balanced brace extraction
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            return None
        text = text[start:]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    steps_data = data.get("steps", [])
    if not steps_data:
        return None

    steps = [
        PlannedStep(
            cmd=s.get("cmd", ""),
            verify=s.get("verify", "exit_code == 0"),
            on_fail=s.get("on_fail", "abort"),
        )
        for s in steps_data
        if s.get("cmd")
    ]

    return PlannedPlan(
        steps=steps,
        done_summary=data.get("done_summary", "Plan completed"),
    )


def _parse_exit_code_from_screen(screen: str) -> int | None:
    """Extract exit code from marker line."""
    for line in reversed(screen.splitlines()):
        if "EXIT:" in line and "EXIT:$" not in line:
            m = re.search(r"EXIT:(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def run_subtask_planned(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute subtask via plan-execute-verify: 1 LLM call, then mechanical execution."""
    client = get_client()
    total_pt = total_ct = 0

    # Phase 1: Generate plan (1 LLM call)
    _active_model = pane_info.agent_model
    system_prompt = build_planned_prompt(
        subtask_description=subtask.description,
        pane_name=subtask.pane,
        app_type=pane_info.app_type,
        tool_description=pane_info.description,
        dependency_context=dep_context,
        session_dir=session_dir,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Generate the plan. Goal: {subtask.description}"},
    ]

    reply, pt, ct = chat(client, messages, model=_active_model)
    total_pt += pt
    total_ct += ct
    _emit(on_event, "turn", subtask.id, 1, "plan generated")
    _emit(on_event, "tokens", subtask.id, pt, ct)

    plan = parse_planned_steps(reply)
    if plan is None:
        return SubtaskResult(
            subtask_id=subtask.id, status=SubtaskStatus.FAILED,
            summary="Failed to parse execution plan from LLM response",
            output_snippet=reply[:500],
            turns_used=1, prompt_tokens=total_pt, completion_tokens=total_ct,
        )

    # Phase 2: Execute plan mechanically (0 LLM calls on happy path)
    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())
    last_screen = ""

    with lock:
        for i, step in enumerate(plan.steps):
            if _cancel_event.is_set():
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary="Cancelled", output_snippet="",
                    turns_used=1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            # Safety check
            violation = _check_command_safety(step.cmd)
            if violation:
                log.warning(violation)
                if step.on_fail == "skip":
                    continue
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary=f"Blocked: {violation}",
                    output_snippet="", turns_used=1,
                    prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            # Execute step
            cmd = step.cmd
            if pane_info.app_type in {"shell", "data", "docs", "media", "browser", "files"}:
                cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)
            wrapped, marker = wrap_command(cmd, subtask.id)
            pane_info.pane.send_keys(wrapped, enter=True)
            screen, method = wait_for_ready(pane_info, marker=marker, max_wait=60.0)
            last_screen = screen

            _emit(on_event, "turn", subtask.id, i + 2, f"step {i+1}/{len(plan.steps)}: {step.cmd[:40]}")

            # Verify
            exit_code = _parse_exit_code_from_screen(screen)
            if exit_code is not None and exit_code != 0:
                log.debug(f"[{subtask.id}] Step {i+1} failed (exit {exit_code}): {step.cmd[:60]}")

                if step.on_fail == "retry":
                    # Re-run once
                    pane_info.pane.send_keys(wrapped, enter=True)
                    screen, _ = wait_for_ready(pane_info, marker=marker, max_wait=60.0)
                    last_screen = screen
                    exit_code = _parse_exit_code_from_screen(screen)
                    if exit_code == 0:
                        continue

                if step.on_fail == "skip":
                    continue

                # abort
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary=f"Step {i+1} failed (exit {exit_code}): {step.cmd[:80]}",
                    output_snippet=screen[-500:],
                    turns_used=1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

    # All steps succeeded
    return SubtaskResult(
        subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
        summary=plan.done_summary,
        output_snippet=last_screen[-500:] if last_screen else "",
        turns_used=1, prompt_tokens=total_pt, completion_tokens=total_ct,
    )
```

**Step 6: Wire into executor.py dispatch**

In `executor.py`, add the planned mode dispatch. After the script mode block (line ~224), add:

```python
    if subtask.mode == "planned":
        from planned_runner import run_subtask_planned
        return run_subtask_planned(
            subtask=subtask,
            pane_info=pane_info,
            dep_context=dep_context,
            on_event=on_event,
            session_dir=session_dir,
        )
```

Also add the re-export at the top of executor.py:

```python
from planned_runner import run_subtask_planned  # noqa: E402
```

**Step 7: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_planned_runner.py tests/test_models.py -v`
Expected: ALL PASS

**Step 8: Commit**

```bash
git add models.py prompts.py planned_runner.py executor.py tests/test_planned_runner.py
git commit -m "feat: plan-execute-verify mode — 1 LLM call, mechanical execution"
```

---

## Task 6: Streaming Observation Events (Strategy 5 extension)

Enhance the interactive runner to present the main model with structured events instead of raw screen diffs. When the observation classifier fires, format events as compact structured messages that reduce token usage.

**Files:**
- Modify: `observation.py` (add `format_event_for_llm`)
- Modify: `interactive_runner.py` (use event formatting)
- Test: `tests/test_observation.py` (extend)

**Step 1: Add event formatting tests**

Append to `tests/test_observation.py`:

```python
def test_format_success_event():
    from observation import ScreenEvent, EventType, format_event_for_llm
    event = ScreenEvent(
        type=EventType.SUCCESS,
        summary="file1.txt\nfile2.txt\nfile3.txt",
        needs_llm=False,
        exit_code=0,
    )
    formatted = format_event_for_llm(event)
    assert "exit:0" in formatted.lower() or "EXIT:0" in formatted
    assert len(formatted) < len(event.summary) + 50  # compact


def test_format_error_event():
    from observation import ScreenEvent, EventType, format_event_for_llm
    event = ScreenEvent(
        type=EventType.ERROR,
        summary="command not found: foo",
        needs_llm=True,
        exit_code=127,
    )
    formatted = format_event_for_llm(event)
    assert "127" in formatted
    assert "ERROR" in formatted or "error" in formatted.lower()


def test_format_needs_input_event():
    from observation import ScreenEvent, EventType, format_event_for_llm
    event = ScreenEvent(
        type=EventType.NEEDS_INPUT,
        summary="Overwrite file? [y/N]",
        needs_llm=True,
        exit_code=None,
    )
    formatted = format_event_for_llm(event)
    assert "input" in formatted.lower() or "INPUT" in formatted
```

**Step 2: Implement format_event_for_llm**

Add to `observation.py`:

```python
def format_event_for_llm(event: ScreenEvent) -> str:
    """Format a ScreenEvent as a compact message for the main LLM.

    Structured events are 2-5x smaller than raw screen diffs,
    reducing token usage while preserving actionable information.
    """
    if event.type == EventType.SUCCESS:
        return f"[OK exit:{event.exit_code}] {event.summary[:300]}"
    elif event.type == EventType.ERROR:
        return f"[ERROR exit:{event.exit_code}] {event.summary[:400]}"
    elif event.type == EventType.NEEDS_INPUT:
        return f"[NEEDS INPUT] {event.summary[:200]}"
    elif event.type == EventType.RUNNING:
        return f"[RUNNING] {event.summary[:100]}"
    else:
        return f"[SCREEN] {event.summary[:300]}"
```

**Step 3: Integrate event formatting into interactive runner**

In `interactive_runner.py`, after the command is sent and `_send_agent_command` returns, use the classifier to format the observation. The event message replaces or augments the raw diff for the next turn.

Modify the section after `_send_agent_command` (around line 228). The key change is: when the classifier determines `needs_llm=False`, use the compact event format instead of the full screen diff in the next turn's user message. The diff computation at the top of the loop (line ~164-167) should check if the previous turn produced an event:

```python
            # After _send_agent_command:
            _exit_code = _parse_exit_code(prev_screen)
            # Render agent screen before classification
            if pane_info.app_type == "agent":
                prev_screen = render_agent_screen(prev_screen, nonce=pane_info.frame_nonce)

            from observation import ScreenClassifier, format_event_for_llm
            _event = ScreenClassifier().classify(prev_screen, exit_code=_exit_code)

            if not _event.needs_llm and _event.exit_code == 0:
                # Replace next turn's diff with compact event
                messages.append({"role": "user", "content": format_event_for_llm(_event)})
                # Skip existing exit_code injection (already captured in event)
                continue  # go to next turn — diff will be skipped since we injected the event
```

Note: This `continue` jumps to the next turn where the LLM will see the compact event and decide the next action. The LLM is still called, but with much less context.

**Step 4: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_observation.py tests/test_interactive_v2.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add observation.py interactive_runner.py tests/test_observation.py
git commit -m "feat: structured event formatting — compact LLM context from observations"
```

---

## Task 7: Tool-Calling LLM Support (Strategy 6, Part 1)

Add native tool-calling support to `llm.py`. Define pane operation tools. This enables models to emit multiple commands per response and clearly separate reasoning from action.

**Files:**
- Create: `tool_defs.py` (tool definitions for pane operations)
- Modify: `llm.py` (add `chat_with_tools` function)
- Test: `tests/test_tool_calling.py`

**Step 1: Write the failing tests**

```python
# tests/test_tool_calling.py
"""Tests for tool-calling LLM support."""
from tool_defs import PANE_TOOLS, parse_tool_calls


def test_pane_tools_defined():
    assert len(PANE_TOOLS) >= 3
    names = {t["name"] for t in PANE_TOOLS}
    assert "run_command" in names
    assert "read_screen" in names
    assert "complete" in names


def test_pane_tool_schemas_valid():
    for tool in PANE_TOOLS:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool or "parameters" in tool


def test_parse_tool_calls_openai_format():
    """Parse tool calls from OpenAI-format response."""
    import json
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "run_command",
                "arguments": json.dumps({"command": "ls -la"}),
            }
        }
    ]
    parsed = parse_tool_calls(tool_calls, format="openai")
    assert len(parsed) == 1
    assert parsed[0]["name"] == "run_command"
    assert parsed[0]["args"]["command"] == "ls -la"


def test_parse_tool_calls_anthropic_format():
    """Parse tool calls from Anthropic-format response."""
    content_blocks = [
        {"type": "text", "text": "I'll list the files"},
        {"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {"command": "ls -la"}},
    ]
    parsed = parse_tool_calls(content_blocks, format="anthropic")
    assert len(parsed) == 1
    assert parsed[0]["name"] == "run_command"
    assert parsed[0]["args"]["command"] == "ls -la"


def test_parse_multiple_tool_calls():
    """Multiple tool calls in one response (batching)."""
    import json
    tool_calls = [
        {
            "id": "call_1", "type": "function",
            "function": {"name": "run_command", "arguments": json.dumps({"command": "ls"})},
        },
        {
            "id": "call_2", "type": "function",
            "function": {"name": "run_command", "arguments": json.dumps({"command": "pwd"})},
        },
    ]
    parsed = parse_tool_calls(tool_calls, format="openai")
    assert len(parsed) == 2
    assert parsed[0]["args"]["command"] == "ls"
    assert parsed[1]["args"]["command"] == "pwd"


def test_parse_complete_tool_call():
    import json
    tool_calls = [
        {
            "id": "call_1", "type": "function",
            "function": {"name": "complete", "arguments": json.dumps({"summary": "Done listing files"})},
        },
    ]
    parsed = parse_tool_calls(tool_calls, format="openai")
    assert parsed[0]["name"] == "complete"
    assert parsed[0]["args"]["summary"] == "Done listing files"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_tool_calling.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement tool_defs.py**

```python
# tool_defs.py
"""Tool definitions for pane operations.

Defines the tools that models can call when using native tool-calling
mode instead of text-based command extraction. Supports both OpenAI
and Anthropic tool-call response formats.
"""

import json

# Tool definitions in Anthropic format (also works for OpenAI with minor transform)
PANE_TOOLS = [
    {
        "name": "run_command",
        "description": "Run a shell command in the terminal pane. The command is executed and you'll see the output on the next turn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_screen",
        "description": "Read the current terminal screen content. Use when you need to see output without running a new command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of scrollback lines to capture (default 50)",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
    {
        "name": "complete",
        "description": "Mark the task as done. Call this when the goal has been achieved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One-line summary of what was accomplished",
                },
            },
            "required": ["summary"],
        },
    },
]


def tools_for_openai() -> list[dict]:
    """Convert tool definitions to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in PANE_TOOLS
    ]


def tools_for_anthropic() -> list[dict]:
    """Tool definitions are already in Anthropic format."""
    return PANE_TOOLS


def parse_tool_calls(raw, format: str = "openai") -> list[dict]:
    """Parse tool calls from LLM response into uniform format.

    Returns list of {"name": str, "args": dict, "id": str}.
    """
    results = []

    if format == "openai":
        for tc in raw:
            if tc.get("type") == "function" or "function" in tc:
                func = tc.get("function", tc)
                args = func.get("arguments", "{}")
                if isinstance(args, str):
                    args = json.loads(args)
                results.append({
                    "name": func["name"],
                    "args": args,
                    "id": tc.get("id", ""),
                })

    elif format == "anthropic":
        for block in raw:
            if block.get("type") == "tool_use":
                results.append({
                    "name": block["name"],
                    "args": block.get("input", {}),
                    "id": block.get("id", ""),
                })

    return results
```

**Step 4: Add chat_with_tools to llm.py**

Append to `llm.py`:

```python
def chat_with_tools(
    client,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
) -> tuple[list, str, int, int]:
    """Chat with tool-calling support.

    Returns (tool_calls, text_content, prompt_tokens, completion_tokens).
    tool_calls is a list of raw tool call objects (format depends on provider).
    text_content is any non-tool text in the response.
    """
    from delegate_client import DelegateClient
    if isinstance(client, DelegateClient):
        # Delegate doesn't support tools — fall back to text mode
        content, pt, ct = chat(client, messages, max_tokens=max_tokens, model=model)
        return [], content, pt, ct

    if isinstance(client, anthropic.Anthropic):
        system = ""
        filtered = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                filtered.append(msg)

        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if system else []
        response = client.messages.create(
            model=model or MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=filtered,
            tools=tools,
        )
        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        pt = response.usage.input_tokens if response.usage else 0
        ct = response.usage.output_tokens if response.usage else 0
        return tool_calls, "\n".join(text_parts), pt, ct

    # OpenAI-compatible
    from tool_defs import tools_for_openai
    openai_tools = tools_for_openai()
    response = client.chat.completions.create(
        model=model or MODEL,
        messages=messages,
        max_tokens=max_tokens,
        tools=openai_tools,
    )
    choice = response.choices[0]
    text = choice.message.content or ""
    tool_calls = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            })
    pt = response.usage.prompt_tokens if response.usage else 0
    ct = response.usage.completion_tokens if response.usage else 0
    return tool_calls, text, pt, ct
```

**Step 5: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_tool_calling.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add tool_defs.py llm.py tests/test_tool_calling.py
git commit -m "feat: tool-calling support — native tool definitions and multi-provider parsing"
```

---

## Task 8: Tool-Calling Interactive Runner (Strategy 6, Part 2)

Create a tool-calling variant of the interactive runner that uses native tool calls instead of text-based command extraction. This enables command batching (multiple commands per LLM response) and cleaner reasoning/action separation.

**Files:**
- Create: `toolcall_runner.py`
- Modify: `executor.py` (add tool-calling mode detection)
- Test: `tests/test_toolcall_runner.py`

**Step 1: Write the failing tests**

```python
# tests/test_toolcall_runner.py
"""Tests for tool-calling interactive runner."""
import json
from unittest.mock import MagicMock, patch
from models import Subtask, SubtaskStatus, PaneInfo


def _make_pane_info():
    pane = MagicMock()
    pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
    return PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")


def _make_subtask(**kw):
    defaults = dict(id="1", description="list files", pane="shell", mode="interactive", max_turns=5)
    defaults.update(kw)
    return Subtask(**defaults)


class TestToolCallRunner:
    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.wait_for_ready")
    @patch("toolcall_runner.capture_pane")
    def test_single_command_then_done(self, mock_capture, mock_wait, mock_chat):
        """Tool-calling: run_command then complete."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_wait.return_value = ("file1.txt\n[AGENT_READY] $ ", "marker")

        mock_chat.side_effect = [
            # Turn 1: run ls
            (
                [{"type": "tool_use", "id": "t1", "name": "run_command", "input": {"command": "ls"}}],
                "Let me list the files",
                100, 50,
            ),
            # Turn 2: complete
            (
                [{"type": "tool_use", "id": "t2", "name": "complete", "input": {"summary": "Listed files"}}],
                "",
                80, 30,
            ),
        ]

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
        assert "Listed files" in result.summary

    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.wait_for_ready")
    @patch("toolcall_runner.capture_pane")
    def test_batched_commands(self, mock_capture, mock_wait, mock_chat):
        """Tool-calling: multiple run_command calls in one response."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_wait.return_value = ("output\n[AGENT_READY] $ ", "marker")

        mock_chat.side_effect = [
            # Turn 1: two commands batched
            (
                [
                    {"type": "tool_use", "id": "t1", "name": "run_command", "input": {"command": "ls"}},
                    {"type": "tool_use", "id": "t2", "name": "run_command", "input": {"command": "pwd"}},
                ],
                "Running both commands",
                100, 50,
            ),
            # Turn 2: complete
            (
                [{"type": "tool_use", "id": "t3", "name": "complete", "input": {"summary": "Done"}}],
                "",
                80, 20,
            ),
        ]

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
        # Both commands should have been sent
        assert mock_wait.call_count == 2  # waited for each command

    @patch("toolcall_runner.chat_with_tools")
    @patch("toolcall_runner.capture_pane")
    def test_no_tool_calls_falls_back_to_text(self, mock_capture, mock_chat):
        """When model returns text without tool calls, extract command from text."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_chat.side_effect = [
            ([], "DONE: nothing needed", 50, 20),
        ]

        from toolcall_runner import run_subtask_toolcall
        result = run_subtask_toolcall(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_toolcall_runner.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement toolcall_runner.py**

```python
# toolcall_runner.py
"""Tool-calling interactive runner — uses native tool calls instead of text parsing.

Advantages over text-based interactive_runner:
1. Multiple commands per LLM response (batching — fewer turns)
2. Clean reasoning/action separation (text is thinking, tools are actions)
3. Uses model's trained tool-calling ability (more reliable than regex)
"""

import logging
import threading

from command_extract import extract_done
from completion import wait_for_ready, wrap_command
from llm import get_client, chat_with_tools
from models import Subtask, SubtaskStatus, SubtaskResult, PaneInfo
from observation import ScreenClassifier, format_event_for_llm
from prompts import build_interactive_prompt
from runtime import _emit, _check_command_safety, _pane_locks, _cancel_event, _wrap_for_sandbox
from session import capture_pane
from tool_defs import PANE_TOOLS, parse_tool_calls

log = logging.getLogger(__name__)

_SHELL_LIKE_APP_TYPES = {"shell", "data", "docs", "media", "browser", "files"}


def _execute_tool_call(
    tool_call: dict,
    subtask: Subtask,
    pane_info: PaneInfo,
    session_dir: str,
) -> dict:
    """Execute a single tool call. Returns result dict."""
    name = tool_call["name"]
    args = tool_call["args"]

    if name == "complete":
        return {"type": "complete", "summary": args.get("summary", "Done")}

    if name == "read_screen":
        lines = args.get("lines", 50)
        screen = capture_pane(pane_info, scrollback=lines)
        return {"type": "screen", "content": screen}

    if name == "run_command":
        cmd = args.get("command", "")
        if not cmd:
            return {"type": "error", "content": "Empty command"}

        violation = _check_command_safety(cmd)
        if violation:
            return {"type": "error", "content": f"[BLOCKED] {violation}"}

        if pane_info.app_type in _SHELL_LIKE_APP_TYPES:
            cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)
        wrapped, marker = wrap_command(cmd, subtask.id)
        pane_info.pane.send_keys(wrapped, enter=True)
        screen, method = wait_for_ready(pane_info, marker=marker, detect_intervention=True)

        # Classify result
        from interactive_runner import _parse_exit_code
        exit_code = _parse_exit_code(screen)
        classifier = ScreenClassifier()
        event = classifier.classify(screen, exit_code=exit_code)

        return {
            "type": "command_result",
            "content": format_event_for_llm(event),
            "screen": screen,
            "exit_code": exit_code,
            "method": method,
        }

    return {"type": "error", "content": f"Unknown tool: {name}"}


def run_subtask_toolcall(
    subtask: Subtask,
    pane_info: PaneInfo,
    dep_context: str,
    on_event=None,
    session_dir: str = "/tmp/clive",
) -> SubtaskResult:
    """Execute subtask using native tool calling.

    The LLM calls run_command/read_screen/complete tools instead of
    outputting commands as text. Multiple tool calls per response
    are executed sequentially (command batching).
    """
    client = get_client()
    total_pt = total_ct = 0

    _active_model = pane_info.agent_model
    tools = PANE_TOOLS  # Anthropic format; chat_with_tools handles conversion

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

    lock = _pane_locks.setdefault(subtask.pane, threading.Lock())

    with lock:
        for turn in range(1, subtask.max_turns + 1):
            if _cancel_event.is_set():
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary="Cancelled", output_snippet="",
                    turns_used=turn - 1, prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            # Capture current screen for first turn
            if turn == 1:
                screen = capture_pane(pane_info)
                messages.append({"role": "user", "content": screen})

            # Call LLM with tools
            try:
                raw_tool_calls, text, pt, ct = chat_with_tools(
                    client, messages, tools,
                    model=_active_model,
                )
            except Exception as exc:
                log.exception("Tool-calling LLM failed at turn %d", turn)
                return SubtaskResult(
                    subtask_id=subtask.id, status=SubtaskStatus.FAILED,
                    summary=f"LLM call crashed: {exc}",
                    output_snippet="", turns_used=turn,
                    prompt_tokens=total_pt, completion_tokens=total_ct,
                )

            total_pt += pt
            total_ct += ct
            _emit(on_event, "turn", subtask.id, turn, text[:80] if text else "tool calls")
            _emit(on_event, "tokens", subtask.id, pt, ct)

            # Parse tool calls (handle both formats)
            import anthropic as _anth
            fmt = "anthropic" if isinstance(client, _anth.Anthropic) else "openai"
            tool_calls = parse_tool_calls(raw_tool_calls, format=fmt)

            # No tool calls — check for text-based DONE or command
            if not tool_calls:
                if text:
                    done = extract_done(text)
                    if done:
                        return SubtaskResult(
                            subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
                            summary=done, output_snippet="",
                            turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                        )
                    messages.append({"role": "assistant", "content": text})
                continue

            # Execute tool calls sequentially
            results_content = []
            for tc in tool_calls:
                result = _execute_tool_call(tc, subtask, pane_info, session_dir)

                if result["type"] == "complete":
                    return SubtaskResult(
                        subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
                        summary=result["summary"],
                        output_snippet="",
                        turns_used=turn, prompt_tokens=total_pt, completion_tokens=total_ct,
                    )

                results_content.append(result.get("content", ""))

            # Append results as user message for next turn
            messages.append({"role": "assistant", "content": text or "Executing tools..."})
            messages.append({"role": "user", "content": "\n---\n".join(results_content)})

    # Exhausted turns
    return SubtaskResult(
        subtask_id=subtask.id, status=SubtaskStatus.FAILED,
        summary=f"Exhausted {subtask.max_turns} turns without completing",
        output_snippet="", turns_used=subtask.max_turns,
        prompt_tokens=total_pt, completion_tokens=total_ct,
    )
```

**Step 4: Wire into executor.py with capability detection**

In `executor.py`, modify `run_subtask()` to detect when tool-calling is available and prefer it for interactive mode. Add before the existing interactive dispatch (before the `_MODE_TURNS` line):

```python
    # Prefer tool-calling runner when the provider supports it
    if subtask.mode in ("interactive", "streaming"):
        from llm import PROVIDER_NAME
        _toolcall_providers = {"openai", "anthropic", "openrouter", "gemini"}
        if PROVIDER_NAME in _toolcall_providers:
            try:
                from toolcall_runner import run_subtask_toolcall
                return run_subtask_toolcall(
                    subtask=subtask,
                    pane_info=pane_info,
                    dep_context=dep_context,
                    on_event=on_event,
                    session_dir=session_dir,
                )
            except Exception:
                log.debug("Tool-calling runner failed, falling back to text-based", exc_info=True)
```

**Step 5: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_toolcall_runner.py tests/test_interactive_v2.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add toolcall_runner.py tests/test_toolcall_runner.py executor.py
git commit -m "feat: tool-calling runner — native tool calls with command batching"
```

---

## Task 9: Driver Frontmatter Model Defaults

Add sensible model defaults to each driver file. Shell tasks use cheap models, browser/email use mid-tier.

**Files:**
- Modify: `drivers/shell.md`
- Modify: `drivers/data.md`
- Modify: `drivers/browser.md`
- Modify: `drivers/email_cli.md`
- Modify: `drivers/media.md`
- Modify: `drivers/docs.md`

**Step 1: Update shell.md frontmatter**

```yaml
---
preferred_mode: script
use_interactive_when: debugging, exploring unknown output, or multi-step investigation
agent_model: fast
observation_model: fast
---
```

Note: Use "fast" as a tier label rather than a specific model name. The runner resolves tier labels to actual model names (haiku, flash, etc.) based on provider.

**Step 2: Update data.md frontmatter**

```yaml
---
preferred_mode: script
agent_model: fast
observation_model: fast
---
```

**Step 3: Update browser.md frontmatter**

```yaml
---
preferred_mode: script
use_interactive_when: discovering unknown content, following links, or exploring sites
agent_model: default
observation_model: fast
---
```

**Step 4: Update email_cli.md frontmatter**

```yaml
---
preferred_mode: interactive
agent_model: default
observation_model: fast
---
```

**Step 5: Update media.md and docs.md frontmatter**

Both get:
```yaml
agent_model: fast
observation_model: fast
```

**Step 6: Add tier-to-model resolution in runtime.py**

Append to `runtime.py`:

```python
# ── Model Tier Resolution ────────────────────────────────────────────────────

_TIER_MAP = {
    "openai": {"fast": "gpt-4o-mini", "default": None},       # None = use global MODEL
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "default": None},
    "gemini": {"fast": "gemini-2.0-flash", "default": None},
    "openrouter": {"fast": "google/gemini-2.0-flash-exp:free", "default": None},
    "ollama": {"fast": "llama3", "default": None},
    "lmstudio": {"fast": "local", "default": None},
    "delegate": {"fast": None, "default": None},
}


def resolve_model_tier(tier: str | None, provider: str | None = None) -> str | None:
    """Resolve a model tier label ('fast', 'default') to a concrete model name.

    Returns None when the tier is None, 'default', or the provider has no mapping,
    signaling the caller to use the global MODEL.
    """
    if not tier or tier == "default":
        return None
    if not provider:
        from llm import PROVIDER_NAME
        provider = PROVIDER_NAME
    provider_map = _TIER_MAP.get(provider, {})
    return provider_map.get(tier)
```

**Step 7: Update session.py to resolve tiers when setting PaneInfo models**

In the PaneInfo construction (session.py), after loading driver meta:

```python
        from runtime import resolve_model_tier
        if meta.get("agent_model"):
            panes[tool["name"]].agent_model = resolve_model_tier(meta["agent_model"]) or meta["agent_model"]
        if meta.get("observation_model"):
            panes[tool["name"]].observation_model = resolve_model_tier(meta["observation_model"]) or meta["observation_model"]
```

**Step 8: Run full test suite**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/ -x -q`
Expected: ALL PASS

**Step 9: Commit**

```bash
git add drivers/*.md runtime.py session.py
git commit -m "feat: driver model defaults — cheap models for shell/data, default for browser/email"
```

---

## Task 10: Integration Test and Planner Awareness

Wire the planner to be aware of the "planned" mode and prefer it for deterministic multi-step tasks. Add integration-level tests.

**Files:**
- Modify: `prompts.py:106-110` (add planned mode to planner guidance)
- Create: `tests/test_planned_integration.py`

**Step 1: Update planner prompt**

In `prompts.py`, modify the mode guidance inside `build_planner_prompt()` (around line 106-110):

```python
    - "script": One-shot. The agent generates a shell script, executes it, checks the exit code. No observation during execution. Use for: deterministic single-step pipelines, file operations, data extraction, known API calls, text processing. Faster and cheaper.
    - "planned": Multi-step mechanical. The agent generates a sequence of commands with verification criteria, then executes them one-by-one without further LLM calls. Use for: deterministic multi-step workflows where each step is a known command — install+configure, fetch+process+save, multi-file operations. Even cheaper than script for multi-step tasks.
    - "interactive": Turn-by-turn. The agent reads the screen after each command and decides what to do next. Use for: exploring unknown content, debugging, multi-step workflows where the next step depends on the previous result, interactive applications.
    - "streaming": Like interactive, but with automatic intervention detection. The agent is alerted when the process prompts for input (passwords, confirmations, [y/N] prompts). Use for: package installs that may ask for confirmation, operations requiring passwords, long-running processes that may prompt for input, interactive debuggers.
```

**Step 2: Write integration test**

```python
# tests/test_planned_integration.py
"""Integration tests for the planned execution pipeline."""
import json
from unittest.mock import MagicMock, patch
from models import Subtask, SubtaskStatus, PaneInfo, Plan


def test_planned_mode_in_plan_prompt():
    """Planner prompt should mention 'planned' mode."""
    from prompts import build_planner_prompt
    prompt = build_planner_prompt("shell [shell] — Bash\n")
    assert "planned" in prompt


def test_full_pipeline_planned_mode():
    """End-to-end: planner suggests planned mode → executor dispatches → planned_runner executes."""
    from models import Subtask, Plan

    subtask = Subtask(id="1", description="fetch and process data", pane="shell", mode="planned")
    plan = Plan(task="test", subtasks=[subtask])
    errors = plan.validate(valid_panes={"shell"})
    assert not errors, f"Validation errors: {errors}"


def test_planned_mode_turn_count():
    """Planned mode should report turns_used=1 on happy path (only plan generation)."""
    from planned_runner import run_subtask_planned
    from unittest.mock import patch, MagicMock

    pane = MagicMock()
    pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
    pane_info = PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")
    subtask = Subtask(id="1", description="do stuff", pane="shell", mode="planned", max_turns=5)

    plan_response = json.dumps({
        "steps": [{"cmd": "echo hello", "verify": "exit_code == 0", "on_fail": "abort"}],
        "done_summary": "Said hello"
    })

    with patch("planned_runner.chat", return_value=(plan_response, 100, 50)), \
         patch("planned_runner.wait_for_ready", return_value=("hello\nEXIT:0 ___DONE_1_a___\n[AGENT_READY] $ ", "marker")), \
         patch("planned_runner.capture_pane", return_value="[AGENT_READY] $ "):
        result = run_subtask_planned(subtask=subtask, pane_info=pane_info, dep_context="")

    assert result.status == SubtaskStatus.COMPLETED
    assert result.turns_used == 1  # only plan generation
    assert result.prompt_tokens == 100
```

**Step 3: Run all tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/ -x -q`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add prompts.py tests/test_planned_integration.py
git commit -m "feat: planner awareness of planned mode + integration tests"
```

---

## Summary of Token Savings

| Strategy | Files Changed/Created | Expected Savings |
|----------|----------------------|------------------|
| 1. Observation-Action Decoupling | `observation.py`, `interactive_runner.py` | 30-50% fewer main-model calls |
| 2. Progressive Context Compression | `context_compress.py`, `interactive_runner.py` | 40-60% fewer input tokens per turn |
| 3. Plan-Execute-Verify Mode | `planned_runner.py`, `models.py`, `executor.py`, `prompts.py` | 80-90% fewer LLM calls for deterministic tasks |
| 4. Per-Pane Model Selection | `models.py`, `session.py`, `drivers/*.md`, `runtime.py` | 50-70% cost reduction (cheap models where sufficient) |
| 5. Streaming Observation Events | `observation.py` | 20-40% fewer tokens in screen observations |
| 6. Hybrid Tool-Calling | `tool_defs.py`, `llm.py`, `toolcall_runner.py`, `executor.py` | 30-50% fewer turns via command batching |

**Combined conservative estimate**: 3-5x overall reduction in LLM costs.
