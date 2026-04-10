# Pane Core Refocus — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refocus clive on its core identity: a professional terminal operator that translates user intent into CLI commands and scripts.

**Core Principle:** Clive is a skilled engineer working on behalf of a non-technical user. The user says *what* they want. Clive translates that into *how* — the exact CLI commands, pipes, and scripts a professional would type. Then runs them, reads the output, adjusts if needed, and reports back in plain language.

**The professional's workflow:**
1. **Understand** — what does the user actually want?
2. **Translate** — pick the right commands (ls, gh, jq, curl, awk...), compose them
3. **Script if needed** — multi-step logic becomes a bash/python script
4. **Run** — type it into the pane, read the screen
5. **Adjust if it fails** — read the error, try a different approach
6. **Report** — translate terminal output back to what the user understands

**Architecture:** Script mode is the primary path — a professional writes a script, runs it, checks the result. Interactive mode is the exception — for exploration, debugging, unknown territory where the next step depends on what you see. The XML command protocol, PaneAgent/SharedBrain, and side-channel file operations are deleted. The DAG scheduler, completion detection, screen diff, command safety, and drivers are preserved.

**Execution mode hierarchy (maps to the professional model):**
- **Direct**: user typed a command → just run it (zero LLM)
- **Script**: I know what to do → write it, run it, check, repair if needed (1-3 LLM calls)
- **Interactive**: I need to see what's there first → observe-act loop (exception path)
- **Plan**: too big for one script → break into scripts (DAG of the above)

**Tech Stack:** Python 3, libtmux, existing LLM abstraction (llm.py)

---

## Task 1: New command extraction — replace XML parser

**Files:**
- Create: `command_extract.py`
- Test: `tests/test_command_extract.py`

**Step 1: Write the failing tests**

```python
# tests/test_command_extract.py
"""Tests for plain-text command extraction (replaces XML parsing)."""
import pytest
from command_extract import extract_command, extract_done


class TestExtractDone:
    def test_done_at_start(self):
        assert extract_done("DONE: fetched 3 files") == "fetched 3 files"

    def test_done_after_text(self):
        reply = "All good.\nDONE: wrote results to output.csv"
        assert extract_done(reply) == "wrote results to output.csv"

    def test_no_done(self):
        assert extract_done("ls -la\nsome output") is None

    def test_done_empty(self):
        assert extract_done("DONE:") == ""

    def test_done_with_leading_space(self):
        assert extract_done("DONE:  trimmed") == "trimmed"


class TestExtractCommand:
    def test_fenced_bash(self):
        reply = "Let me check.\n```bash\nls -la /tmp\n```\n"
        assert extract_command(reply) == "ls -la /tmp"

    def test_fenced_sh(self):
        reply = "```sh\ngrep -r TODO .\n```"
        assert extract_command(reply) == "grep -r TODO ."

    def test_fenced_no_lang(self):
        reply = "```\ncat file.txt\n```"
        assert extract_command(reply) == "cat file.txt"

    def test_fenced_multiline(self):
        reply = "```bash\nmkdir -p /tmp/out\ncp *.txt /tmp/out/\n```"
        assert extract_command(reply) == "mkdir -p /tmp/out\ncp *.txt /tmp/out/"

    def test_dollar_prefix(self):
        reply = "Run this:\n$ curl -s https://example.com"
        assert extract_command(reply) == "curl -s https://example.com"

    def test_bare_command(self):
        reply = "ls -la /tmp/clive"
        assert extract_command(reply) == "ls -la /tmp/clive"

    def test_skip_comments(self):
        reply = "# This is a plan\nls /tmp"
        assert extract_command(reply) == "ls /tmp"

    def test_done_returns_none(self):
        reply = "DONE: all finished"
        assert extract_command(reply) is None

    def test_empty(self):
        assert extract_command("") is None

    def test_only_prose(self):
        # Prose-only reply — no clear command
        reply = "I think we should check the logs first."
        cmd = extract_command(reply)
        # Should return the first line as best guess (LLM rarely does this)
        assert cmd is not None

    def test_fenced_python_ignored(self):
        """Python blocks are not shell commands — skip them."""
        reply = "```python\nprint('hello')\n```"
        assert extract_command(reply) is None

    def test_fenced_bash_preferred_over_bare(self):
        reply = "I suggest:\n```bash\nfind . -name '*.py'\n```\nAlternatively: ls"
        assert extract_command(reply) == "find . -name '*.py'"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_command_extract.py -v`
Expected: FAIL — module not found

**Step 3: Write implementation**

```python
# command_extract.py
"""Plain-text command extraction from LLM replies.

Replaces the XML <cmd> protocol. The LLM just types what it would
type at a terminal. Commands are extracted from fenced code blocks
or bare lines.
"""
import re

_FENCED_RE = re.compile(
    r'```(?:bash|sh)?\s*\n(.*?)```', re.DOTALL
)
_FENCED_PYTHON_RE = re.compile(
    r'```python[3]?\s*\n.*?```', re.DOTALL
)
_DONE_RE = re.compile(r'^DONE:\s*(.*)', re.MULTILINE)

# Lines that are clearly not commands
_SKIP_PREFIXES = ('#', '//', 'DONE:', '> ')


def extract_done(reply: str) -> str | None:
    """Extract completion summary from DONE: marker. Returns None if not found."""
    m = _DONE_RE.search(reply)
    if m:
        return m.group(1).strip()
    return None


def extract_command(reply: str) -> str | None:
    """Extract shell command from LLM reply.

    Priority:
    1. DONE: marker → return None (task complete, no command)
    2. Fenced ```bash or ```sh block → return contents
    3. Line starting with $ → return remainder
    4. First non-comment, non-empty line → return as command
    """
    if not reply or not reply.strip():
        return None

    # 1. DONE signal — no command to execute
    if extract_done(reply) is not None:
        return None

    # 2. Fenced bash/sh code block (preferred)
    m = _FENCED_RE.search(reply)
    if m:
        return m.group(1).strip()

    # 2b. Fenced block with no language tag (but not python)
    m = re.search(r'```\s*\n(.*?)```', reply, re.DOTALL)
    if m:
        content = m.group(1).strip()
        # Reject if it looks like python
        if not content.startswith(('import ', 'from ', 'def ', 'class ', 'print(')):
            return content

    # 3. $ prefix
    for line in reply.splitlines():
        stripped = line.strip()
        if stripped.startswith('$ '):
            return stripped[2:]

    # 4. First non-skip line
    for line in reply.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _SKIP_PREFIXES):
            continue
        return stripped

    return None
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_command_extract.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add command_extract.py tests/test_command_extract.py
git commit -m "feat: plain-text command extraction replacing XML protocol"
```

---

## Task 2: New prompts — the professional operator

The script prompt is the **core** prompt — it's where the professional's expertise lives. The interactive prompt is the **exception** — for observation-dependent decisions.

**Files:**
- Modify: `prompts.py` (add `build_script_prompt_v2` and `build_interactive_prompt_v2`, keep old for now)
- Test: `tests/test_prompts_v2.py`

**Step 1: Write the failing tests**

```python
# tests/test_prompts_v2.py
"""Tests for the refocused prompts."""
from prompts import build_script_prompt_v2, build_interactive_prompt_v2


class TestScriptPromptV2:
    def test_contains_professional_framing(self):
        p = build_script_prompt_v2("count files", "shell", "shell", "Bash shell", "")
        assert "professional" in p.lower() or "skilled" in p.lower() or "engineer" in p.lower()

    def test_contains_task(self):
        p = build_script_prompt_v2("count .py files", "shell", "shell", "Bash shell", "")
        assert "count .py files" in p

    def test_contains_driver(self):
        p = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        assert "bash" in p.lower() or "shell" in p.lower()

    def test_no_xml_tags(self):
        p = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        assert "<cmd" not in p

    def test_contains_session_dir(self):
        p = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell", "",
                                   session_dir="/tmp/clive/abc")
        assert "/tmp/clive/abc" in p

    def test_shorter_than_old(self):
        from prompts import build_script_prompt
        old = build_script_prompt("do stuff", "shell", "shell", "Bash shell", "")
        new = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        # v2 should be comparable or shorter — not bloated
        assert len(new) < len(old) * 1.2

    def test_dep_context_included(self):
        p = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell",
                                   "Dep [1] DONE: got 3 files")
        assert "got 3 files" in p


class TestInteractivePromptV2:
    def test_contains_observation_framing(self):
        p = build_interactive_prompt_v2("explore logs", "shell", "shell", "Bash shell", "")
        # Should frame as observation/investigation, not "autonomous agent"
        assert "observe" in p.lower() or "screen" in p.lower() or "see" in p.lower()

    def test_contains_done_signal(self):
        p = build_interactive_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        assert "DONE:" in p

    def test_no_xml_tags(self):
        p = build_interactive_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        assert "<cmd" not in p
        assert "</cmd>" not in p

    def test_contains_session_dir(self):
        p = build_interactive_prompt_v2("do stuff", "shell", "shell", "Bash shell", "",
                                       session_dir="/tmp/clive/abc")
        assert "/tmp/clive/abc" in p

    def test_much_shorter_than_old_worker(self):
        from prompts import build_worker_prompt
        old = build_worker_prompt("do stuff", "shell", "shell", "Bash shell", "")
        new = build_interactive_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        assert len(new) < len(old) * 0.7  # at least 30% shorter
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_prompts_v2.py -v`
Expected: FAIL — functions not found

**Step 3: Write implementation**

Add to `prompts.py`:

```python
def build_script_prompt_v2(
    subtask_description: str,
    pane_name: str,
    app_type: str,
    tool_description: str,
    dependency_context: str,
    session_dir: str = "/tmp/clive",
) -> str:
    """Script prompt — the core. A professional writes a script, runs it once."""
    dep_section = ""
    if dependency_context:
        dep_section = f"""
Context from prior steps:
{dependency_context}
"""

    driver = load_driver(app_type)

    import platform
    os_name = platform.system()
    os_arch = platform.machine()
    os_info = f"OS: {os_name} ({os_arch})"
    if os_name == "Darwin":
        os_info += "\nIMPORTANT: macOS with BSD coreutils. Use POSIX-compatible commands."

    return f"""You are a skilled engineer writing a script for: {subtask_description}

Pane: {pane_name} [{app_type}] — {tool_description}
{os_info}

{driver}
{dep_section}
Write a single self-contained script. Choose bash or Python — whichever fits best.
- Bash: start with #!/bin/bash and use set -euo pipefail
- Python: start with #!/usr/bin/env python3
- Read input from the current working directory (relative paths)
- Write output/results to {session_dir}/ (absolute paths)
- Print a short preview of output + one-line summary as last line

Respond with ONLY the script in a fenced code block. No prose.
"""


def build_interactive_prompt_v2(
    subtask_description: str,
    pane_name: str,
    app_type: str,
    tool_description: str,
    dependency_context: str,
    session_dir: str = "/tmp/clive",
) -> str:
    """Interactive prompt — the exception. For when you need to see before you act."""
    dep_section = ""
    if dependency_context:
        dep_section = f"""
Prior results:
{dependency_context}
"""

    driver = load_driver(app_type)

    return f"""You control pane "{pane_name}" [{app_type}] — {tool_description}

{driver}

GOAL: {subtask_description}
{dep_section}
You're investigating something where the next step depends on what you see.
Type one command. Read the screen output (shown next turn). Decide what's next.
Write results to {session_dir}/
When done: DONE: <one-line summary>
"""
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_prompts_v2.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add prompts.py tests/test_prompts_v2.py
git commit -m "feat: terminal-native worker prompt (v2)"
```

---

## Task 3: New interactive worker — the 60-line core

**Files:**
- Modify: `executor.py` (add `run_subtask_interactive` alongside existing `run_subtask`)
- Test: `tests/test_interactive_v2.py`

**Step 1: Write the failing tests**

```python
# tests/test_interactive_v2.py
"""Tests for the refocused interactive worker."""
import pytest
from unittest.mock import MagicMock, patch
from models import Subtask, SubtaskResult, SubtaskStatus, PaneInfo


def _make_pane_info():
    pane = MagicMock()
    pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
    return PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")


def _make_subtask(**kw):
    defaults = dict(id="1", description="list files", pane="shell", mode="interactive", max_turns=5)
    defaults.update(kw)
    return Subtask(**defaults)


class TestInteractiveV2:
    @patch("executor.chat")
    @patch("executor.capture_pane")
    @patch("executor.wait_for_ready")
    def test_single_command_then_done(self, mock_wait, mock_capture, mock_chat):
        """LLM sends a command, then DONE on next turn."""
        mock_capture.side_effect = [
            "[AGENT_READY] $ ",  # turn 1: initial screen
            "file1.txt\nfile2.txt\n[AGENT_READY] $ ",  # turn 2: after ls
        ]
        mock_chat.side_effect = [
            ("```bash\nls\n```", 100, 50),  # turn 1: command
            ("DONE: found 2 files", 100, 30),  # turn 2: done
        ]
        mock_wait.return_value = ("file1.txt\nfile2.txt\n[AGENT_READY] $ ", "marker")

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
        assert "2 files" in result.summary

    @patch("executor.chat")
    @patch("executor.capture_pane")
    def test_done_on_first_reply(self, mock_capture, mock_chat):
        """LLM immediately says DONE (trivial task)."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_chat.return_value = ("DONE: nothing to do", 50, 20)

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED

    @patch("executor.chat")
    @patch("executor.capture_pane")
    def test_exhausted_turns(self, mock_capture, mock_chat):
        """Worker exhausts turns without DONE."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_chat.return_value = ("```bash\nls\n```", 100, 50)

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(max_turns=2),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.FAILED
        assert "turns" in result.summary.lower()

    @patch("executor.chat")
    @patch("executor.capture_pane")
    def test_blocked_command(self, mock_capture, mock_chat):
        """Dangerous command gets blocked, worker continues."""
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_chat.side_effect = [
            ("```bash\nrm -rf /\n```", 100, 50),  # dangerous
            ("DONE: aborted", 50, 20),
        ]

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=_make_subtask(max_turns=3),
            pane_info=_make_pane_info(),
            dep_context="",
        )
        assert result.status == SubtaskStatus.COMPLETED
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_interactive_v2.py -v`
Expected: FAIL — function not found

**Step 3: Write implementation**

Add to `executor.py`:

```python
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
    from prompts import build_interactive_prompt_v2

    client = get_client()
    total_pt = total_ct = 0

    system_prompt = build_interactive_prompt_v2(
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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_interactive_v2.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add executor.py tests/test_interactive_v2.py
git commit -m "feat: refocused interactive worker — plain text, no XML"
```

---

## Task 4: Wire v2 worker into the dispatch path

**Files:**
- Modify: `executor.py` — update `run_subtask()` to use `run_subtask_interactive` for interactive/streaming modes

**Step 1: Write the integration test**

```python
# tests/test_dispatch_v2.py
"""Verify run_subtask dispatches interactive mode to v2 worker."""
from unittest.mock import patch, MagicMock
from models import Subtask, SubtaskStatus, PaneInfo


def test_interactive_dispatches_to_v2():
    """Interactive mode should use run_subtask_interactive."""
    subtask = Subtask(id="1", description="test", pane="shell", mode="interactive")
    pane_info = PaneInfo(
        pane=MagicMock(), app_type="shell", description="Bash", name="shell"
    )

    with patch("executor.run_subtask_interactive") as mock_v2:
        mock_v2.return_value = MagicMock(status=SubtaskStatus.COMPLETED)
        from executor import run_subtask
        run_subtask(subtask=subtask, pane_info=pane_info, dep_context="")
        mock_v2.assert_called_once()


def test_script_still_works():
    """Script mode should NOT use v2 worker."""
    subtask = Subtask(id="1", description="test", pane="shell", mode="script")
    pane_info = PaneInfo(
        pane=MagicMock(), app_type="shell", description="Bash", name="shell"
    )

    with patch("executor.run_subtask_script") as mock_script:
        mock_script.return_value = MagicMock(status=SubtaskStatus.COMPLETED)
        from executor import run_subtask
        run_subtask(subtask=subtask, pane_info=pane_info, dep_context="")
        mock_script.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dispatch_v2.py -v`
Expected: FAIL — still dispatches to old path

**Step 3: Modify `run_subtask` dispatch**

In `executor.py`, replace the interactive/streaming section of `run_subtask` (everything after the `if subtask.mode == "script"` block) with:

```python
    # Interactive and streaming modes → v2 worker
    return run_subtask_interactive(
        subtask=subtask,
        pane_info=pane_info,
        dep_context=dep_context,
        on_event=on_event,
        session_dir=session_dir,
    )
```

Keep the skill runner check, direct mode, and script mode dispatches unchanged.

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_dispatch_v2.py tests/test_interactive_v2.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add executor.py tests/test_dispatch_v2.py
git commit -m "feat: wire v2 interactive worker into dispatch path"
```

---

## Task 5: Delete PaneAgent and SharedBrain

**Files:**
- Delete: `pane_agent.py`
- Modify: `executor.py` — remove all PaneAgent/SharedBrain usage from `execute_plan`
- Delete: `tests/test_pane_agent.py` (if exists)

**Step 1: Remove PaneAgent from execute_plan**

In `executor.py::execute_plan()`, remove:
- The `from pane_agent import PaneAgent, SharedBrain` import
- The `shared_brain = SharedBrain(session_dir)` creation
- The `agent_state_dir` loading
- The `pane_agents` dict creation and population
- The `if agent:` branch in the submit-to-pool section (keep only the direct `run_subtask` path)
- The agent state persistence at the end (`agent.save(...)`, `shared_brain.save(...)`)

Replace the submit block with:

```python
future = pool.submit(
    run_subtask,
    subtask=subtask,
    pane_info=panes[subtask.pane],
    dep_context=dep_context,
    on_event=on_event,
    session_dir=session_dir,
)
```

**Step 2: Delete pane_agent.py**

```bash
git rm pane_agent.py
```

**Step 3: Run existing tests**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_pane_agent.py -x`
Expected: PASS (no tests should depend on PaneAgent)

**Step 4: Clean up any test files**

```bash
git rm tests/test_pane_agent.py 2>/dev/null || true
```

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove PaneAgent and SharedBrain — pane scrollback is the memory"
```

---

## Task 6: Delete old interactive worker code

**Files:**
- Modify: `executor.py` — remove the old 330-line interactive loop, XML parsing, file channel
- Delete or update: `tests/test_parse_command.py`, `tests/test_integration.py` (XML-specific tests)

**Step 1: Remove from executor.py**

Delete these functions:
- `parse_command()` — XML parser
- `parse_commands()` — multi-XML parser
- `read_file()` — file channel read
- `write_file()` — file channel write (keep if used by script mode — check first)
- The old interactive loop body (everything from line ~976 to ~1352 in the original, now replaced by `run_subtask_interactive`)
- `_write_recovery_pattern()` — scratchpad writes

Keep:
- `run_subtask_direct()` — zero-LLM path
- `run_subtask_script()` — script mode
- `run_subtask_interactive()` — new v2 worker
- `execute_plan()` — DAG scheduler
- `_check_command_safety()` — safety
- `_extract_script()` — script extraction
- `_trim_messages()` — context management
- `_try_collapse_plan()` — plan optimization
- `_build_plan_context()` — plan context
- `_build_dependency_context()` — dep context
- `_track_result_files()` — file tracking
- `_cancel_orphaned_branches()` — branch cancel

Note: `write_file()` is used by `run_subtask_script()` to write scripts. Check if it can use `os.makedirs + open()` inline instead. If script mode calls `write_file()`, keep it as a private helper `_write_file()`.

**Step 2: Update tests**

- Remove `tests/test_parse_command.py` (XML-specific)
- Update `tests/test_integration.py` to remove XML parsing tests, keep `_extract_script` and `_build_dependency_context` tests

**Step 3: Run all tests**

Run: `python3 -m pytest tests/ -v -x`
Expected: PASS

**Step 4: Commit**

```bash
git add -A
git commit -m "refactor: remove XML command protocol and old interactive loop"
```

---

## Task 7: Clean up prompts — remove v1, rename v2

**Files:**
- Modify: `prompts.py` — delete old `build_worker_prompt` and `build_script_prompt`, rename v2 variants to standard names
- Modify: `executor.py` — update imports
- Modify: `tests/test_prompts_v2.py` — update function names

**Step 1: Rename in prompts.py**

- Delete `build_worker_prompt` → replaced by `build_interactive_prompt_v2`
- Delete `build_script_prompt` → replaced by `build_script_prompt_v2`
- Rename `build_script_prompt_v2` → `build_script_prompt`
- Rename `build_interactive_prompt_v2` → `build_interactive_prompt`

**Step 2: Update all callers**

```bash
grep -rn "build_worker_prompt\|build_script_prompt\|build_interactive_prompt" --include="*.py" .
```

Update `executor.py`, `evals/harness/run_eval.py`, and any other callers.

**Step 3: Run all tests**

Run: `python3 -m pytest tests/ -v -x`
Expected: PASS

**Step 4: Commit**

```bash
git add -A
git commit -m "refactor: finalize prompts — script is core, interactive is exception"
```

---

## Task 8: Update the script prompt to remove XML references

**Files:**
- Modify: `prompts.py` — `build_script_prompt` (minimal, already clean)

The script prompt is already mostly clean (it asks for a fenced code block). Just verify no XML references leaked in.

**Step 1: Verify**

Run: `grep -n '<cmd' prompts.py`
Expected: no matches after cleanup

**Step 2: Commit if changes needed**

```bash
git add prompts.py
git commit -m "refactor: ensure no XML protocol references in prompts"
```

---

## Task 9: Update eval harness for v2

**Files:**
- Modify: `evals/harness/run_eval.py` — ensure it calls `run_subtask` correctly (it already does via the dispatch, so this may be a no-op)

**Step 1: Verify eval harness still works**

Run: `python3 evals/harness/run_eval.py --layer 3 --tool script_correctness --ci`
Expected: Tests run and pass at similar rate to before

**Step 2: Commit if changes needed**

---

## Task 10: Deep eval scenarios

**Files:**
- Create: `evals/layer2/shell_v2/tasks.json` + `fixtures/`
- Create: `evals/layer3/interactive_core/tasks.json` + `fixtures/`

These are NEW eval scenarios specifically designed to stress-test the refocused core: plain-text command extraction, the read-think-type loop, multi-turn reasoning from pane scrollback, and error recovery WITHOUT side channels.

### Eval Suite A: Pure Pane Interaction (Layer 2)

These test that the agent can work entirely through the pane — no file channel crutches.

```json
[
  {
    "id": "pane_core_read_and_transform_001",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Read the file data.csv, count the number of rows (excluding header), and write just the count to /tmp/clive/result.txt",
    "initial_state": {"filesystem": "fixtures/csv_simple/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "test \"$(cat /tmp/clive/result.txt | tr -d '[:space:]')\" = \"5\""
    },
    "min_turns": 2,
    "max_turns": 5,
    "timeout_seconds": 30
  },
  {
    "id": "pane_core_error_recovery_002",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Read the file data.json and extract the 'email' field from each entry. Write emails one per line to /tmp/clive/result.txt. Note: the file might have some malformed entries — skip those.",
    "initial_state": {"filesystem": "fixtures/json_with_errors/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "grep -c '@' /tmp/clive/result.txt | grep -q '[3-5]'"
    },
    "min_turns": 2,
    "max_turns": 6,
    "timeout_seconds": 30
  },
  {
    "id": "pane_core_multi_step_003",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Find the largest file in the current directory (by size), then count the number of lines in that file, and write 'FILENAME:LINECOUNT' to /tmp/clive/result.txt",
    "initial_state": {"filesystem": "fixtures/mixed_files/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "grep -q ':' /tmp/clive/result.txt && test -s /tmp/clive/result.txt"
    },
    "min_turns": 2,
    "max_turns": 5,
    "timeout_seconds": 30
  },
  {
    "id": "pane_core_observe_adapt_004",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Check if jq is installed. If yes, use jq to extract names from data.json. If not, use python3 instead. Write names one per line to /tmp/clive/result.txt.",
    "initial_state": {"filesystem": "fixtures/json_sum/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "test \"$(wc -l < /tmp/clive/result.txt | tr -d ' ')\" -ge 2"
    },
    "min_turns": 2,
    "max_turns": 6,
    "timeout_seconds": 30
  },
  {
    "id": "pane_core_heredoc_write_005",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Create a JSON file at /tmp/clive/config.json with the contents: {\"host\": \"localhost\", \"port\": 8080, \"debug\": true}. Then verify it's valid JSON and write 'VALID' or 'INVALID' to /tmp/clive/result.txt.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "test \"$(cat /tmp/clive/result.txt | tr -d '[:space:]')\" = \"VALID\" && python3 -c \"import json; json.load(open('/tmp/clive/config.json'))\""
    },
    "min_turns": 2,
    "max_turns": 5,
    "timeout_seconds": 30
  }
]
```

### Eval Suite B: Scrollback-as-Context (Layer 3)

These test that the agent reasons correctly from what's on screen — the key differentiator of the pane-as-session-store model.

```json
[
  {
    "id": "scrollback_error_chain_001",
    "layer": 3,
    "tool": "shell",
    "mode": "interactive",
    "task": "Run the script process.sh. It will fail. Read the error message from the screen, diagnose the issue, fix the script, and run it again. Write 'FIXED' to /tmp/clive/result.txt when it succeeds.",
    "initial_state": {"filesystem": "fixtures/broken_script/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "test \"$(cat /tmp/clive/result.txt | tr -d '[:space:]')\" = \"FIXED\""
    },
    "min_turns": 3,
    "max_turns": 8,
    "timeout_seconds": 45
  },
  {
    "id": "scrollback_progressive_discovery_002",
    "layer": 3,
    "tool": "shell",
    "mode": "interactive",
    "task": "A file called mystery.dat exists. Figure out what format it is (use 'file' command), then extract its contents appropriately and write a one-line summary of what it contains to /tmp/clive/result.txt.",
    "initial_state": {"filesystem": "fixtures/mystery_file/"},
    "success_criteria": {
      "type": "llm",
      "prompt": "Does the result.txt contain a reasonable summary of the file contents? The file is a gzipped CSV with 3 columns: name, age, city."
    },
    "min_turns": 3,
    "max_turns": 8,
    "timeout_seconds": 45
  },
  {
    "id": "scrollback_long_output_003",
    "layer": 3,
    "tool": "shell",
    "mode": "interactive",
    "task": "Run 'find / -name \"*.conf\" -maxdepth 3 2>/dev/null | head -20'. From the output, pick the first .conf file that exists, read its first 5 lines, and write those lines to /tmp/clive/result.txt.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "test -s /tmp/clive/result.txt && test \"$(wc -l < /tmp/clive/result.txt | tr -d ' ')\" -le 6"
    },
    "min_turns": 3,
    "max_turns": 6,
    "timeout_seconds": 30
  },
  {
    "id": "scrollback_iterative_refinement_004",
    "layer": 3,
    "tool": "shell",
    "mode": "interactive",
    "task": "Write an awk script that processes access.log to count requests per HTTP status code. If the first attempt produces wrong output (check by comparing with expected.txt), adjust and retry. Write final output to /tmp/clive/result.txt.",
    "initial_state": {"filesystem": "fixtures/access_log/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "diff <(sort /tmp/clive/result.txt) <(sort expected.txt) > /dev/null 2>&1"
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 45
  },
  {
    "id": "scrollback_env_awareness_005",
    "layer": 3,
    "tool": "shell",
    "mode": "interactive",
    "task": "Check the OS type (Linux or macOS). Based on the result, use the correct command to list network interfaces (ip addr on Linux, ifconfig on macOS). Write the name of the first non-loopback interface to /tmp/clive/result.txt.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "test -s /tmp/clive/result.txt && ! grep -qi 'lo$' /tmp/clive/result.txt"
    },
    "min_turns": 2,
    "max_turns": 5,
    "timeout_seconds": 30
  }
]
```

### Eval Suite C: Script Mode Purity (Layer 3)

Verify script mode still generates correct one-shot scripts without the XML protocol.

```json
[
  {
    "id": "script_pure_pipeline_001",
    "layer": 3,
    "tool": "shell",
    "mode": "script",
    "task": "Read input.csv (has columns: name,department,salary). Calculate the average salary per department. Write result as CSV (department,avg_salary) to /tmp/clive/result.txt, sorted by department name.",
    "initial_state": {"filesystem": "fixtures/salary_data/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "head -1 /tmp/clive/result.txt | grep -qi 'department' && test \"$(wc -l < /tmp/clive/result.txt | tr -d ' ')\" -ge 3"
    },
    "min_turns": 1,
    "max_turns": 3,
    "timeout_seconds": 30
  },
  {
    "id": "script_pure_multifile_002",
    "layer": 3,
    "tool": "shell",
    "mode": "script",
    "task": "Merge all .json files in the current directory into a single JSON array. Each file contains one JSON object. Write the merged array to /tmp/clive/result.txt.",
    "initial_state": {"filesystem": "fixtures/json_fragments/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "python3 -c \"import json; d=json.load(open('/tmp/clive/result.txt')); assert isinstance(d, list) and len(d) >= 3\""
    },
    "min_turns": 1,
    "max_turns": 3,
    "timeout_seconds": 30
  },
  {
    "id": "script_pure_conditional_003",
    "layer": 3,
    "tool": "shell",
    "mode": "script",
    "task": "Check if the file data.xml exists. If yes, extract all <name> tag values using grep/sed and write them to /tmp/clive/result.txt. If no, write 'NO_XML_FILE' to /tmp/clive/result.txt.",
    "initial_state": {"filesystem": "fixtures/xml_data/"},
    "success_criteria": {
      "type": "deterministic",
      "check": "test -s /tmp/clive/result.txt && (grep -q 'Alice' /tmp/clive/result.txt || grep -q 'NO_XML_FILE' /tmp/clive/result.txt)"
    },
    "min_turns": 1,
    "max_turns": 3,
    "timeout_seconds": 30
  }
]
```

### Eval Suite D: Regression — Ensure Nothing Broke (Layer 2)

Re-run existing L2/L3 evals under the new worker. This is not a new task file — it's a test step:

```bash
# Run all existing evals and compare
python3 evals/harness/run_eval.py --all --output /tmp/clive_v2_results.json
# Compare against baseline
python3 evals/harness/run_eval.py --all --baseline previous_baseline.json
```

**Step 1: Create fixture directories and files for new evals**

For each fixture referenced above, create the necessary test data files. Examples:

- `fixtures/csv_simple/data.csv`: 5-row CSV with header
- `fixtures/json_with_errors/data.json`: JSON array with one malformed entry
- `fixtures/mixed_files/`: 3-4 files of different sizes
- `fixtures/broken_script/process.sh`: Script with a deliberate syntax error
- `fixtures/mystery_file/mystery.dat`: gzipped CSV
- `fixtures/access_log/access.log` + `expected.txt`: Apache-style log
- `fixtures/salary_data/input.csv`: name,department,salary data
- `fixtures/json_fragments/`: 3+ individual `.json` files
- `fixtures/xml_data/data.xml`: Simple XML with `<name>` tags

**Step 2: Register eval tasks**

Place each tasks.json in its directory under `evals/layer2/` or `evals/layer3/`.

**Step 3: Run and verify**

```bash
python3 evals/harness/run_eval.py --layer 2 --tool shell_v2
python3 evals/harness/run_eval.py --layer 3 --tool interactive_core
python3 evals/harness/run_eval.py --layer 3 --tool script_pure
```

**Step 4: Commit**

```bash
git add evals/
git commit -m "eval: deep eval scenarios for pane-core refocus"
```

---

## Task 11: Run full regression + baseline

**Step 1: Capture baseline before refactor (optional — if old code still on a branch)**

```bash
git stash
python3 evals/harness/run_eval.py --all --output evals/baselines/pre_refocus.json
git stash pop
```

**Step 2: Run full eval suite with new code**

```bash
python3 evals/harness/run_eval.py --all --output evals/baselines/post_refocus.json
```

**Step 3: Compare**

```bash
python3 evals/harness/run_eval.py --all --baseline evals/baselines/pre_refocus.json
```

Key metrics to track:
- **Pass rate**: should be equal or better (fewer protocol misunderstandings)
- **Turn efficiency**: should improve (no wasted turns on XML formatting errors)
- **Token usage**: should decrease (shorter prompts, no XML overhead)
- **Cost**: should decrease proportionally

**Step 4: Commit baseline**

```bash
git add evals/baselines/
git commit -m "eval: baseline comparison pre/post pane-core refocus"
```

---

## Summary: What Gets Deleted

| Component | Lines | Reason |
|-----------|-------|--------|
| `pane_agent.py` | 343 | Pane scrollback is the memory |
| `parse_command()` + `parse_commands()` | 55 | XML protocol replaced by plain text |
| `read_file()` / `write_file()` in executor | 20 | LLM uses `cat`, `heredoc` like a human |
| Old interactive loop | 330 | Replaced by 60-line v2 |
| XML command types in worker prompt | ~30 | Gone — "you're at a terminal" |
| Scratchpad/recovery pattern writes | 15 | Files in session_dir suffice |
| `save_skill` command type | 15 | Orchestrator concern, not worker |
| `peek` command type | 15 | Cross-pane via files |
| **Total removed** | **~820** | |
| **Total added** | **~150** | command_extract.py + v2 worker + v2 prompt |

**Net: −670 lines. The core gets simpler.**
