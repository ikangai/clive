# Script Mode Speedup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Cut trivial script-mode task latency from ~55s to ~15s by eliminating wasted LLM calls (OS mismatch, extraction failures, unnecessary planning).

**Architecture:** Four independent fixes — broader trivial-task detection, OS-aware script prompts, stricter code-block formatting, and an optional fast model for script generation. Each fix is self-contained; together they eliminate the three retry causes observed in the "average file size" benchmark.

**Tech Stack:** Python, pytest, existing clive modules (clive.py, prompts.py, executor.py, llm.py)

---

### Task 1: Broaden trivial-task detection to skip planner for simple questions

The current `_is_trivial()` only matches tasks starting with specific command words (`ls`, `curl`, etc.). Tasks like "what is the average file size in this dir?" don't match, causing a wasted ~10s planning LLM call that produces a single-subtask plan anyway.

**Files:**
- Modify: `clive.py:59-70` (`TRIVIAL_PATTERNS`, `_is_trivial`)
- Test: `tests/test_planner_bypass.py`

**Step 1: Write failing tests**

Add to `tests/test_planner_bypass.py`:

```python
def test_trivial_question_what():
    from clive import _is_trivial
    assert _is_trivial("what is the average file size in this dir?", 1) is True

def test_trivial_question_how_many():
    from clive import _is_trivial
    assert _is_trivial("how many python files are there?", 1) is True

def test_trivial_check():
    from clive import _is_trivial
    assert _is_trivial("check disk usage", 1) is True

def test_trivial_get():
    from clive import _is_trivial
    assert _is_trivial("get the current date", 1) is True

def test_not_trivial_multi_step():
    from clive import _is_trivial
    assert _is_trivial("download the file, parse it, then email the results", 1) is False

def test_not_trivial_and_conjunction():
    from clive import _is_trivial
    assert _is_trivial("list files and email the summary to bob", 1) is False
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_planner_bypass.py -v`
Expected: 4 new tests FAIL (the "not trivial" tests pass, the "trivial" ones fail)

**Step 3: Implement broader detection**

In `clive.py`, replace the `TRIVIAL_PATTERNS` list and `_is_trivial` function:

```python
TRIVIAL_PATTERNS = [
    _re.compile(r'^(list|count|find|show|cat|head|tail|wc|grep|ls|du|df|check|get|print|echo|stat|file|type|which|where|uname|date|whoami|hostname|pwd|id)\b', _re.IGNORECASE),
    _re.compile(r'^(curl|wget|fetch)\s+\S+$', _re.IGNORECASE),
    _re.compile(r'^(what|how many|how much|how big|how large|what is|what are)\b', _re.IGNORECASE),
]

# Tasks with conjunctions ("and", "then") that suggest multi-step work
_MULTI_STEP_SIGNALS = _re.compile(r'\b(and then|then|&&|\band\b.*\b(email|send|upload|post|write to|save to))', _re.IGNORECASE)

def _is_trivial(task: str, num_panes: int) -> bool:
    """Detect tasks that don't need planning."""
    if num_panes > 1:
        return False
    if len(task.split()) > 20:
        return False
    if _MULTI_STEP_SIGNALS.search(task):
        return False
    return any(p.search(task.strip()) for p in TRIVIAL_PATTERNS)
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_planner_bypass.py -v`
Expected: ALL tests PASS

**Step 5: Commit**

```bash
git add clive.py tests/test_planner_bypass.py
git commit -m "feat: broaden trivial-task detection to skip planner for questions"
```

---

### Task 2: Inject OS info into script prompt to prevent macOS/Linux mismatch

The LLM generates `find -printf` and other GNU-only commands on macOS because the prompt gives no OS context. Adding `uname` output eliminates this entire class of retry.

**Files:**
- Modify: `prompts.py:172-221` (`build_script_prompt`)
- Test: `tests/test_script_mode.py`

**Step 1: Write failing tests**

Add to `tests/test_script_mode.py`:

```python
def test_script_prompt_contains_os_info():
    import platform
    prompt = build_script_prompt(
        subtask_description="List files",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="",
        session_dir="/tmp/clive/test",
    )
    assert platform.system() in prompt
    assert "Platform" in prompt or "OS" in prompt


def test_script_prompt_macos_warning():
    """On macOS, the prompt should warn about GNU vs BSD differences."""
    import platform
    prompt = build_script_prompt(
        subtask_description="Find files",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="",
        session_dir="/tmp/clive/test",
    )
    if platform.system() == "Darwin":
        assert "BSD" in prompt or "macOS" in prompt or "gnu" in prompt.lower()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_script_mode.py -v`
Expected: new tests FAIL

**Step 3: Implement OS injection**

In `prompts.py`, modify `build_script_prompt` to add OS context. Add at the top of the function body:

```python
import platform
os_name = platform.system()  # "Darwin", "Linux", etc.
os_arch = platform.machine()  # "arm64", "x86_64", etc.
os_info = f"OS: {os_name} ({os_arch})"
if os_name == "Darwin":
    os_info += "\nIMPORTANT: This is macOS with BSD coreutils. Do NOT use GNU extensions like find -printf, sed -i without '', xargs -d, etc. Use POSIX-compatible or macOS alternatives."
```

Then inject `{os_info}` into the prompt string between the tool knowledge and goal sections:

```
{os_info}

Your goal:
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_script_mode.py -v`
Expected: ALL tests PASS

**Step 5: Commit**

```bash
git add prompts.py tests/test_script_mode.py
git commit -m "feat: inject OS info into script prompt to prevent GNU/BSD mismatch"
```

---

### Task 3: Stricter code-block formatting requirement in script prompt

The first attempt in the benchmark failed because the LLM returned prose without a code block, causing `_extract_script` to raise. Strengthen the prompt instruction.

**Files:**
- Modify: `prompts.py:172-221` (`build_script_prompt` — the closing instruction)
- Test: `tests/test_script_mode.py`

**Step 1: Write failing test**

Add to `tests/test_script_mode.py`:

```python
def test_script_prompt_strict_format_instruction():
    prompt = build_script_prompt(
        subtask_description="Count files",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="",
        session_dir="/tmp/clive/test",
    )
    # Must contain strong formatting instruction
    assert "ONLY" in prompt
    assert "```bash" in prompt or "```python" in prompt
    # Must warn against prose/explanation
    assert "no explanation" in prompt.lower() or "no prose" in prompt.lower() or "nothing else" in prompt.lower()
```

**Step 2: Run test — should already pass (or nearly pass)**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_script_mode.py::test_script_prompt_strict_format_instruction -v`

The existing prompt already says "Respond with ONLY the script inside a code block" — check if the test passes. If it does, strengthen the test to require the new stricter language.

**Step 3: Strengthen the prompt**

Replace the closing instruction in `build_script_prompt` (the section starting "Respond with ONLY"):

```python
Respond with ONLY the script inside a fenced code block. No explanation, no prose, nothing else.
The FIRST line of your response must be ``` — do not write anything before the code block.

```bash
#!/bin/bash
set -euo pipefail
# your script here
```
"""
```

**Step 4: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_script_mode.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add prompts.py tests/test_script_mode.py
git commit -m "feat: stricter code-block formatting in script prompt"
```

---

### Task 4: Optional fast model for script generation

Script generation for simple tasks doesn't need the full reasoning model. Add `SCRIPT_MODEL` env var that defaults to `AGENT_MODEL` but can be overridden with a cheaper/faster model (e.g., `gemini-2.0-flash`, `gpt-4o-mini`).

**Files:**
- Modify: `llm.py:49` (add `SCRIPT_MODEL` constant)
- Modify: `executor.py:219-260` (`run_subtask_script` — pass model to `chat()`)
- Test: `tests/test_script_mode.py`

**Step 1: Write failing test**

Add to `tests/test_script_mode.py`:

```python
def test_script_model_env_var(monkeypatch):
    """SCRIPT_MODEL env var should be exposed from llm module."""
    monkeypatch.setenv("SCRIPT_MODEL", "fast-model-123")
    # Force reimport to pick up env change
    import importlib
    import llm
    importlib.reload(llm)
    assert llm.SCRIPT_MODEL == "fast-model-123"
    # Cleanup: reload with default
    monkeypatch.delenv("SCRIPT_MODEL", raising=False)
    importlib.reload(llm)


def test_script_model_defaults_to_agent_model(monkeypatch):
    """When SCRIPT_MODEL is not set, it should equal MODEL."""
    monkeypatch.delenv("SCRIPT_MODEL", raising=False)
    import importlib
    import llm
    importlib.reload(llm)
    assert llm.SCRIPT_MODEL == llm.MODEL
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_script_mode.py::test_script_model_env_var tests/test_script_mode.py::test_script_model_defaults_to_agent_model -v`
Expected: FAIL (SCRIPT_MODEL not defined)

**Step 3: Add SCRIPT_MODEL to llm.py**

After `MODEL = ...` line (line 49), add:

```python
SCRIPT_MODEL = os.getenv("SCRIPT_MODEL", MODEL)
```

**Step 4: Wire SCRIPT_MODEL into executor**

In `executor.py`, in `run_subtask_script()`, change the `chat()` call (line 259) to pass the model:

```python
from llm import SCRIPT_MODEL
reply, pt, ct = chat(client, messages, model=SCRIPT_MODEL)
```

**Step 5: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_script_mode.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add llm.py executor.py tests/test_script_mode.py
git commit -m "feat: optional SCRIPT_MODEL env var for faster script generation"
```

---

### Task 5: Run full test suite and verify no regressions

**Step 1: Run all tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/ -v --tb=short 2>&1 | tail -40`
Expected: All existing tests still pass, plus all new tests pass.

**Step 2: Commit if any fixups needed**
