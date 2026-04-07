# Read Loop Performance Optimizations

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Cut interactive mode token costs by 50-70% and wall time by 30% through screen diffing, context compression, batched exit checks, and expanded marker-based completion.

**Architecture:** Five changes to the core loop, each independent and testable: (1) `screen_diff()` sends only changed lines to the LLM, (2) `trim_messages()` caps conversation history to recent context, (3) combine script execution + exit code check into one round-trip, (4) use marker-based completion for all shell-like panes, (5) strengthen script-mode guidance in the planner prompt.

**Tech Stack:** Python 3, libtmux, difflib

---

### Task 1: Screen diffing

Send only changed screen content to the LLM, not the full screen every turn.

**Files:**
- Create: `screen_diff.py`
- Create: `tests/test_screen_diff.py`
- Modify: `executor.py:442-469` (interactive loop screen capture)

**Step 1: Write the failing test**

Create `tests/test_screen_diff.py`:

```python
"""Tests for screen diff utility."""
from screen_diff import compute_screen_diff


def test_first_capture_returns_full_screen():
    diff = compute_screen_diff(None, "line1\nline2\nline3")
    assert "line1" in diff
    assert "line2" in diff
    assert "line3" in diff


def test_identical_screens_returns_no_change():
    screen = "$ ls\nfile1.txt\nfile2.txt\n[AGENT_READY] $"
    diff = compute_screen_diff(screen, screen)
    assert "no change" in diff.lower() or "unchanged" in diff.lower()


def test_new_lines_shown():
    prev = "$ ls\n[AGENT_READY] $"
    curr = "$ ls\nfile1.txt\nfile2.txt\n[AGENT_READY] $"
    diff = compute_screen_diff(prev, curr)
    assert "file1.txt" in diff
    assert "file2.txt" in diff


def test_removed_lines_not_included():
    prev = "line1\nline2\nline3"
    curr = "line1\nline3"
    diff = compute_screen_diff(prev, curr)
    # Should show current state, not deletions
    assert "line1" in diff
    assert "line3" in diff


def test_diff_is_shorter_than_full_screen():
    prev = "\n".join(f"line {i}" for i in range(50))
    curr = prev + "\nnew line 50\nnew line 51"
    diff = compute_screen_diff(prev, curr)
    full = curr
    assert len(diff) < len(full)


def test_large_change_returns_full_screen():
    prev = "old content"
    curr = "\n".join(f"new line {i}" for i in range(30))
    diff = compute_screen_diff(prev, curr)
    # When most of the screen changed, just send the full thing
    assert "new line 0" in diff
    assert "new line 29" in diff
```

**Step 2: Run test — expect FAIL**

Run: `python3 -m pytest tests/test_screen_diff.py -v`

**Step 3: Implement screen_diff.py**

Create `screen_diff.py`:

```python
"""Screen diff utility for the read loop.

Instead of sending the full tmux screen every turn, compute what changed
and send only the delta. This cuts token usage by 60-80% after turn 1.

The diff is designed for LLM consumption, not human review:
- First capture: full screen (no prior state)
- No change: "[Screen unchanged]"
- Small change: show only new/changed lines with context
- Large change (>50% different): show full screen (diff would be confusing)
"""
import difflib


def compute_screen_diff(
    prev_screen: str | None,
    curr_screen: str,
    context_lines: int = 1,
) -> str:
    """Compute a compact diff between two screen captures.

    Args:
        prev_screen: Previous screen content (None for first capture)
        curr_screen: Current screen content
        context_lines: Lines of context around changes

    Returns:
        A string suitable for sending to the LLM as screen context.
    """
    # First capture — send everything
    if prev_screen is None:
        return curr_screen

    # No change
    if prev_screen == curr_screen:
        return "[Screen unchanged]"

    prev_lines = prev_screen.splitlines()
    curr_lines = curr_screen.splitlines()

    # Compute unified diff
    diff = list(difflib.unified_diff(
        prev_lines, curr_lines,
        n=context_lines,
        lineterm="",
    ))

    if not diff:
        return "[Screen unchanged]"

    # Count how many lines actually changed
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    total_changed = added + removed

    # If more than 50% of lines changed, send full screen (diff is confusing)
    if total_changed > len(curr_lines) * 0.5:
        return curr_screen

    # Build compact diff output for the LLM
    parts = [f"[Screen update: +{added} -{removed} lines]"]
    for line in diff:
        if line.startswith("@@"):
            continue  # skip diff headers
        if line.startswith("+++") or line.startswith("---"):
            continue  # skip file headers
        if line.startswith("+"):
            parts.append(f"  {line[1:]}")  # new line (no + prefix, cleaner for LLM)
        elif line.startswith(" "):
            parts.append(f"  {line[1:]}")  # context line
        # Skip removed lines — the LLM doesn't need to see what disappeared

    return "\n".join(parts)
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/test_screen_diff.py -v`

**Step 5: Wire into executor.py interactive loop**

In `executor.py`, in the interactive loop (run_subtask), replace the screen capture and context building block. Change:

```python
        for turn in range(1, subtask.max_turns + 1):
            # Capture current pane state
            screen = capture_pane(pane_info)
```

Add `last_screen = None` before the loop, and replace the context building:

```python
    last_screen = None

    with _pane_locks[subtask.pane]:
        for turn in range(1, subtask.max_turns + 1):
            # Capture current pane state
            screen = capture_pane(pane_info)
```

Then change the context building from:

```python
            meta = get_meta(pane_info.pane)
            context = (
                f"[Subtask {subtask.id} Turn {turn}]\n"
                f"[Pane: {subtask.pane}] [Meta: {meta}]\n{screen}"
            )
```

To:

```python
            from screen_diff import compute_screen_diff
            screen_content = compute_screen_diff(last_screen, screen)
            last_screen = screen
            meta = get_meta(pane_info.pane)
            context = (
                f"[Subtask {subtask.id} Turn {turn}]\n"
                f"[Pane: {subtask.pane}] [Meta: {meta}]\n{screen_content}"
            )
```

**Step 6: Run all tests**

Run: `python3 -m pytest tests/ -v`

**Step 7: Commit**

```bash
git add screen_diff.py tests/test_screen_diff.py executor.py
git commit -m "perf: screen diffing — send only changed lines to LLM"
```

---

### Task 2: Context window compression

Cap conversation history to system prompt + last N turns to prevent unbounded growth.

**Files:**
- Create: `tests/test_context_trim.py`
- Modify: `executor.py` (add trim before each chat() call)

**Step 1: Write the failing test**

Create `tests/test_context_trim.py`:

```python
"""Tests for context window trimming."""
from executor import _trim_messages


def test_short_conversation_unchanged():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "reply 1"},
    ]
    trimmed = _trim_messages(messages, max_user_turns=3)
    assert len(trimmed) == 3


def test_long_conversation_trimmed():
    messages = [{"role": "system", "content": "system prompt"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"turn {i}"})
        messages.append({"role": "assistant", "content": f"reply {i}"})
    trimmed = _trim_messages(messages, max_user_turns=3)
    # system + last 3 user-assistant pairs = 7 messages
    assert len(trimmed) == 7
    assert trimmed[0]["role"] == "system"
    assert "turn 9" in trimmed[-2]["content"]  # most recent user
    assert "turn 7" in trimmed[1]["content"]  # oldest kept


def test_preserves_system_prompt():
    messages = [
        {"role": "system", "content": "important system prompt"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
    ]
    trimmed = _trim_messages(messages, max_user_turns=2)
    assert trimmed[0]["content"] == "important system prompt"
    assert "u4" in trimmed[-2]["content"]
    assert "u3" in trimmed[1]["content"]
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement _trim_messages in executor.py**

Add this function before `run_subtask`:

```python
def _trim_messages(messages: list[dict], max_user_turns: int = 4) -> list[dict]:
    """Trim conversation history to system prompt + last N user-assistant pairs.

    Prevents unbounded context growth in the interactive loop.
    Keeps the system prompt and the most recent turns.
    """
    if not messages:
        return messages

    # Separate system prompt from conversation
    system = [m for m in messages if m["role"] == "system"]
    conversation = [m for m in messages if m["role"] != "system"]

    # Count user turns
    user_indices = [i for i, m in enumerate(conversation) if m["role"] == "user"]

    if len(user_indices) <= max_user_turns:
        return messages  # nothing to trim

    # Keep last max_user_turns user messages and everything after the cutoff
    cutoff_idx = user_indices[-max_user_turns]
    trimmed_conversation = conversation[cutoff_idx:]

    return system + trimmed_conversation
```

Then in `run_subtask`'s interactive loop, before the `chat()` call, add:

```python
            # Trim context to prevent unbounded growth
            messages = _trim_messages(messages, max_user_turns=4)
```

**Step 4: Run all tests**

**Step 5: Commit**

```bash
git add executor.py tests/test_context_trim.py
git commit -m "perf: context window compression — cap history to last 4 turns"
```

---

### Task 3: Batch exit code check

Combine script execution and exit code check into one tmux round-trip.

**Files:**
- Modify: `executor.py:170-187` (run_subtask_script)

**Step 1: Replace the two-step execute+check with one step**

In `run_subtask_script`, replace:

```python
            wrapped, marker = wrap_command(f"bash {script_path}", subtask.id)
            pane_info.pane.send_keys(wrapped, enter=True)
            screen, method = wait_for_ready(pane_info, marker=marker, max_wait=60.0)

            progress(f"    [{subtask.id}] Script attempt {attempt}: {screen[-80:]}")

            # Check exit code
            exit_check, exit_marker = wrap_command("echo EXIT:$?", subtask.id)
            pane_info.pane.send_keys(exit_check, enter=True)
            exit_screen, _ = wait_for_ready(pane_info, marker=exit_marker)
```

With:

```python
            # Execute script and capture exit code in one round-trip
            nonce = uuid.uuid4().hex[:4]
            marker = f"___DONE_{subtask.id}_{nonce}___"
            combined = f'bash {script_path}; echo "EXIT:$? {marker}"'
            pane_info.pane.send_keys(combined, enter=True)
            screen, method = wait_for_ready(pane_info, marker=marker, max_wait=60.0)

            progress(f"    [{subtask.id}] Script attempt {attempt}: {screen[-80:]}")
```

And update the exit code parsing to extract from the combined marker line:

```python
            exit_code = None
            for line in screen.splitlines():
                if marker in line and "EXIT:" in line:
                    try:
                        exit_part = line.split("EXIT:")[1].split()[0]
                        exit_code = int(exit_part)
                    except (ValueError, IndexError):
                        pass
```

Add `import uuid` if not already imported (it's in completion.py but not executor.py).

**Step 2: Run all tests**

**Step 3: Commit**

```bash
git add executor.py
git commit -m "perf: batch script execution + exit code into one round-trip"
```

---

### Task 4: Expand marker-based completion to all shell-like panes

Currently marker wrapping only happens when `pane_info.app_type == "shell"`. Extend to all panes that are actually bash (data, docs, media, etc.).

**Files:**
- Modify: `executor.py:498-510` (shell command handling in interactive loop)

**Step 1: Replace the app_type check**

Change:

```python
            elif cmd["type"] == "shell":
                # Wrap shell commands with end marker for reliable detection
                if pane_info.app_type == "shell":
                    wrapped, marker = wrap_command(cmd["value"], subtask.id)
```

To:

```python
            elif cmd["type"] == "shell":
                # Wrap shell commands with end marker for reliable detection
                # All shell-like panes (shell, data, docs, media, browser) benefit from markers
                SHELL_LIKE = {"shell", "data", "docs", "media", "browser", "files"}
                if pane_info.app_type in SHELL_LIKE:
                    wrapped, marker = wrap_command(cmd["value"], subtask.id)
```

**Step 2: Run all tests**

**Step 3: Commit**

```bash
git add executor.py
git commit -m "perf: marker-based completion for all shell-like panes"
```

---

### Task 5: Strengthen script-mode guidance in planner prompt

Push the planner harder toward script mode. Current eval data shows script mode is 2.5x cheaper with the same pass rate.

**Files:**
- Modify: `prompts.py:46-48` (mode guidance in planner prompt)

**Step 1: Update the mode guidance**

Replace the current rule 10 text:

```
    Default to "script" when possible — it's faster and cheaper.
```

With:

```
    STRONGLY prefer "script" — it is 2.5x cheaper and just as reliable. Only use "interactive" when the task explicitly requires reading unknown output, navigating an interactive application, or multi-step exploration where the next step depends on observing the previous result.
```

**Step 2: Run all tests**

**Step 3: Commit**

```bash
git add prompts.py
git commit -m "perf: stronger script-mode guidance in planner prompt"
```

---

## Expected impact

| Optimization | Token savings | Wall time savings |
|---|---|---|
| Screen diffing | 40-60% of screen tokens | None (compute is free) |
| Context compression | 50-70% at turn 5+ | Faster LLM inference (smaller prompt) |
| Batched exit check | 0 tokens | ~2s per script attempt |
| Expanded markers | 0 tokens | ~2s per command on non-shell panes |
| Script-mode push | ~60% (fewer turns = fewer tokens) | ~50% (fewer LLM calls) |

Combined: **50-70% token reduction for interactive tasks, 30-50% wall time reduction overall.**
