# Phase 1: Sub-Agent Specialization + Layer 2 Evals

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make clive's workers tool-aware via auto-discovered driver prompts, add `--quiet` mode for shell-primitive use, and build a Layer 2 eval harness to measure driver prompt quality.

**Architecture:** Replace the generic worker prompt with per-tool "device driver" prompts loaded from `drivers/*.md` files, auto-discovered by `app_type`. Add an output routing module (`output.py`) that separates telemetry (stderr) from results (stdout) when `--quiet` is set. Build an eval harness that creates isolated tmux fixtures and runs tasks against driver prompts with deterministic verification.

**Tech Stack:** Python 3, libtmux, pytest (for unit tests of new modules), tmux (for eval harness)

---

### Task 1: Output routing module

Create `output.py` — a minimal module that routes telemetry to stderr when `--quiet` is set, and final results to stdout always. This replaces bare `print()` calls across the codebase.

**Files:**
- Create: `output.py`
- Create: `tests/test_output.py`

**Step 1: Write the failing test**

Create `tests/test_output.py`:

```python
"""Tests for output routing (quiet mode)."""
import sys
from io import StringIO
from output import progress, result, set_quiet


def test_progress_default_goes_to_stdout(capsys):
    set_quiet(False)
    progress("hello")
    captured = capsys.readouterr()
    assert captured.out.strip() == "hello"
    assert captured.err == ""


def test_progress_quiet_goes_to_stderr(capsys):
    set_quiet(True)
    progress("hello")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "hello"
    set_quiet(False)  # reset


def test_result_always_goes_to_stdout(capsys):
    set_quiet(True)
    result("final answer")
    captured = capsys.readouterr()
    assert captured.out.strip() == "final answer"
    assert captured.err == ""
    set_quiet(False)  # reset
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_output.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'output'`

**Step 3: Write minimal implementation**

Create `output.py`:

```python
"""Output routing for clive.

Separates telemetry (progress) from results:
- Normal mode: both go to stdout
- Quiet mode (--quiet): telemetry to stderr, results to stdout

This enables clive as a shell primitive:
    result=$(clive --quiet "task")   # captures only the result
"""
import sys

_quiet = False


def set_quiet(quiet: bool):
    """Enable/disable quiet mode."""
    global _quiet
    _quiet = quiet


def is_quiet() -> bool:
    """Check if quiet mode is active."""
    return _quiet


def progress(msg: str):
    """Print progress/telemetry. Goes to stderr in quiet mode."""
    print(msg, file=sys.stderr if _quiet else sys.stdout)


def result(msg: str):
    """Print final result. Always goes to stdout."""
    print(msg, file=sys.stdout)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_output.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
git add output.py tests/test_output.py
git commit -m "feat: add output routing module for quiet mode support"
```

---

### Task 2: Replace bare print() calls with progress()

Replace all telemetry `print()` calls in `clive.py`, `executor.py`, `planner.py`, and `session.py` with `progress()`. Keep `result()` for the final summary output in `clive.py`.

**Files:**
- Modify: `clive.py` (lines 58-60, 65-66, 71-72, 82-85, 90, 95, 99, 108-115)
- Modify: `executor.py` (lines 148, 166, 196, 216, 275)
- Modify: `planner.py` (lines 38, 42)
- Modify: `session.py` (line 87)

**Step 1: Update imports in all four files**

In each file, add at the top:
```python
from output import progress, result  # result only needed in clive.py
```

**Step 2: Replace print() calls in clive.py**

Replace every `print()` call in `clive.py` with `progress()`, EXCEPT for the final summary block (lines 108-115) where:
- The decorated banner lines and stats use `progress()`
- The actual summary content (line 111: `print(summary)`) uses `result()`

Specifically, the final block becomes:
```python
    progress(f"\n{'=' * 60}")
    progress(f"TASK COMPLETE ({completed}/{total} subtasks succeeded)")
    progress(f"{'=' * 60}")
    result(summary)
    progress(f"{'~' * 60}")
    progress(f"Time:   {elapsed:.1f}s")
    progress(f"Tokens: {total_pt} prompt + {total_ct} completion = {total_pt + total_ct} total")
    progress(f"{'=' * 60}\n")
```

For all other `print()` calls in `clive.py` (setup info, phase markers), replace with `progress()`.

**Step 3: Replace print() calls in executor.py**

Replace all `print()` calls with `progress()`:
- Line 148: `print(f"  SKIP...")` → `progress(f"  SKIP...")`
- Line 166: `print(f"  START...")` → `progress(f"  START...")`
- Line 196: `print(f"  {status_str}...")` → `progress(f"  {status_str}...")`
- Line 216: `print(f"  WARNING...")` → `progress(f"  WARNING...")`
- Line 275: `print(f"    [{subtask.id}]...")` → `progress(f"    [{subtask.id}]...")`

**Step 4: Replace print() calls in planner.py**

Replace `print()` on lines 38 and 42 with `progress()`. Also replace all `print()` calls in `display_plan()` (lines 82-96) with `progress()`.

**Step 5: Replace print() calls in session.py**

Replace `print()` on line 87 (in `check_health()`) with `progress()`.

**Step 6: Verify nothing broke**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_output.py -v`
Expected: 3 PASSED

Also verify the module loads:
Run: `cd /Users/martintreiber/Documents/Development/clive && python -c "from clive import run; print('OK')"`
Expected: `OK`

**Step 7: Commit**

```bash
git add clive.py executor.py planner.py session.py
git commit -m "refactor: route all telemetry through output.progress()"
```

---

### Task 3: Add --quiet CLI flag

Wire the `--quiet` flag into the CLI entry point so `clive --quiet "task"` sends all telemetry to stderr.

**Files:**
- Modify: `clive.py` (argparse section, lines ~145-199)

**Step 1: Add the --quiet argument**

After the `--safe-mode` argument (line 198), add:

```python
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Quiet mode: telemetry to stderr, only result to stdout",
    )
```

**Step 2: Wire it into set_quiet() before run()**

Before the `run()` call at the end of `__main__` (line 301), add:

```python
    if args.quiet:
        from output import set_quiet
        set_quiet(True)
```

**Step 3: Verify by running with --quiet**

Run: `cd /Users/martintreiber/Documents/Development/clive && python clive.py --quiet --help`
Expected: help text shows `--quiet` / `-q` option

**Step 4: Commit**

```bash
git add clive.py
git commit -m "feat: add --quiet flag for shell-primitive mode"
```

---

### Task 4: Driver auto-discovery

Create the `drivers/` directory and `load_driver()` function. Wire it into `build_worker_prompt()` so the worker prompt includes tool-specific knowledge when a driver file exists.

**Files:**
- Create: `drivers/` directory
- Create: `drivers/default.md` (generic fallback driver)
- Create: `tests/test_drivers.py`
- Modify: `prompts.py` (lines 50-86, `build_worker_prompt()`)

**Step 1: Write the failing test**

Create `tests/test_drivers.py`:

```python
"""Tests for driver prompt auto-discovery."""
import os
import tempfile
from prompts import load_driver


def test_load_existing_driver(tmp_path):
    driver_file = tmp_path / "shell.md"
    driver_file.write_text("# Shell driver\nKEYS: ctrl-c=interrupt")
    result = load_driver("shell", drivers_dir=str(tmp_path))
    assert "Shell driver" in result
    assert "ctrl-c=interrupt" in result


def test_load_missing_driver_returns_default(tmp_path):
    result = load_driver("nonexistent_tool", drivers_dir=str(tmp_path))
    assert result  # should return the default driver, not empty
    assert "autonomous agent" in result.lower() or "worker" in result.lower()


def test_load_driver_from_real_drivers_dir():
    """Once we create drivers/shell.md, this should find it."""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    drivers_dir = os.path.join(project_dir, "drivers")
    if os.path.exists(os.path.join(drivers_dir, "shell.md")):
        result = load_driver("shell", drivers_dir=drivers_dir)
        assert len(result) > 50  # should be a substantial driver prompt
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_drivers.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_driver' from 'prompts'`

**Step 3: Implement load_driver() and update build_worker_prompt()**

Modify `prompts.py`. Add `load_driver()` at the top, and update `build_worker_prompt()` to use it.

Add to `prompts.py` before `build_planner_prompt()`:

```python
import os

# Path to drivers directory (relative to this file)
_DRIVERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drivers")

DEFAULT_DRIVER = """You control this pane via shell commands.
Read the screen output after each command to decide your next action.
If a command fails, read the error and try a different approach."""


def load_driver(app_type: str, drivers_dir: str | None = None) -> str:
    """Load a driver prompt for the given app_type.

    Auto-discovers drivers from the drivers/ directory by matching
    {app_type}.md. Falls back to DEFAULT_DRIVER if no file found.
    """
    base = drivers_dir or _DRIVERS_DIR
    path = os.path.join(base, f"{app_type}.md")
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    return DEFAULT_DRIVER
```

Update `build_worker_prompt()` to inject the driver:

```python
def build_worker_prompt(
    subtask_description: str,
    pane_name: str,
    app_type: str,
    tool_description: str,
    dependency_context: str,
) -> str:
    dep_section = ""
    if dependency_context:
        dep_section = f"""
Results from prerequisite tasks (use this information):
{dependency_context}
"""

    driver = load_driver(app_type)

    return f"""You are an autonomous agent worker controlling a single tmux pane.

Your pane: {pane_name} [{app_type}] — {tool_description}

Tool knowledge:
{driver}

Your goal:
{subtask_description}
{dep_section}
Send exactly one command per turn using XML tags:

  <cmd type="shell" pane="{pane_name}">your command here</cmd>
  <cmd type="read_file" pane="{pane_name}">/path/to/file</cmd>
  <cmd type="write_file" pane="{pane_name}" path="/path/to/file">content</cmd>
  <cmd type="task_complete">summary of what you accomplished</cmd>

Rules:
- One command per turn.
- You can ONLY send commands to pane "{pane_name}".
- Use task_complete when your goal is achieved.
- Write intermediate results to /tmp/clive/ so other tasks can use them.
- read_file and write_file operate on the LOCAL filesystem only. For remote panes, use cat/shell redirects instead.
- If something unexpected happens, describe it in your response and try to recover.
- Silent commands (mkdir, touch) produce no output — this is normal.
"""
```

**Step 4: Create default.md fallback and the drivers/ directory**

```bash
mkdir -p drivers
```

Create `drivers/default.md`:

```markdown
You control this pane via shell commands.
Read the screen output after each command to decide your next action.
If a command fails, read the error and try a different approach.
```

**Step 5: Run tests to verify they pass**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_drivers.py -v`
Expected: 2 PASSED, 1 PASSED or SKIPPED (the real-dir test passes once shell.md exists)

**Step 6: Commit**

```bash
git add prompts.py drivers/default.md tests/test_drivers.py
git commit -m "feat: add driver prompt auto-discovery from drivers/*.md"
```

---

### Task 5: Shell driver prompt

Write `drivers/shell.md` — the device driver for bash shell panes. Compact reference card format, not tutorial. This is the most-used pane type so it has the highest impact.

**Files:**
- Create: `drivers/shell.md`

**Step 1: Write the shell driver**

Create `drivers/shell.md`:

```markdown
# Shell Driver (bash)

ENVIRONMENT: bash shell with PS1="[AGENT_READY] $ "
WORKING DIR: /tmp/clive (shared workspace — write results here)

COMMAND EXECUTION:
- One command per turn. Wait for output before sending next.
- Use && to chain dependent commands: mkdir -p out && cp file out/
- Use ; only when second command should run regardless of first.
- Redirect output to files for other tasks: cmd > /tmp/clive/result.txt

EXIT CODES:
- Check with: cmd; echo "EXIT:$?"
- 0=success, 1=general error, 2=misuse, 126=not executable, 127=not found

PATTERNS:
- Long output: cmd | head -50 or cmd | tail -20
- Search files: grep -r 'pattern' /path or rg 'pattern' /path
- JSON processing: curl -s url | jq '.field'
- CSV processing: mlr --csv filter '$col > val' file.csv
- File listing: ls -la /path (not just ls)
- Disk usage: du -sh /path/*
- Process text: awk, sed, sort, uniq, wc, cut, tr

PITFALLS:
- Quoting: use single quotes for literal strings, double for variable expansion
- Glob expansion: quote patterns when passing to grep/find: grep 'TODO' *.py
- Large directories: pipe ls through head to avoid flooding the screen
- Binary files: use file cmd to check type before cat
- Permissions: if "Permission denied", check with ls -la, try with sudo only if appropriate

COMPLETION: Use <cmd type="task_complete">summary</cmd> when goal is achieved.
Write results to /tmp/clive/ files for other subtasks to consume.
```

**Step 2: Verify it loads correctly**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -c "from prompts import load_driver; d = load_driver('shell'); print(f'Loaded {len(d)} chars'); assert 'EXIT CODES' in d"`
Expected: `Loaded NNN chars` (no assertion error)

**Step 3: Commit**

```bash
git add drivers/shell.md
git commit -m "feat: add shell driver prompt (compact reference card)"
```

---

### Task 6: Browser driver prompt

Write `drivers/browser.md` — the device driver for the browser pane (lynx, curl, wget). This pane uses `app_type: "browser"`.

**Files:**
- Create: `drivers/browser.md`

**Step 1: Write the browser driver**

Create `drivers/browser.md`:

```markdown
# Browser Driver (lynx/curl/wget)

ENVIRONMENT: bash shell configured for web access.
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
  lynx -dump URL          → rendered text output (best for reading pages)
  lynx -listonly URL      → extract all links from a page
  lynx -source URL        → raw HTML source
  curl -s URL             → raw response (best for APIs, JSON)
  curl -sI URL            → headers only (check redirects, content-type)
  wget -q -O file URL     → download to file (best for binary/large files)

LYNX PATTERNS:
- Extract heading: lynx -dump URL | head -20
- Follow link by text: lynx -dump URL | grep -i 'link text'
- Get all links: lynx -listonly -dump URL
- Handle redirects: lynx follows automatically; curl needs -L flag

CURL PATTERNS:
- JSON API: curl -s URL | jq '.field'
- POST data: curl -s -X POST -H 'Content-Type: application/json' -d '{"key":"val"}' URL
- Auth header: curl -s -H 'Authorization: Bearer TOKEN' URL
- Follow redirects: curl -sL URL
- Save response: curl -s URL > /tmp/clive/response.json

PITFALLS:
- lynx -dump on large pages: pipe through head -100 to avoid flooding screen
- curl without -s: progress bar clutters output, always use -s (silent)
- HTTPS errors: use curl -sk to skip cert verification only if needed
- Binary content: check Content-Type with curl -sI before dumping
- Rate limiting: add sleep 1 between rapid API calls

OUTPUT: Save extracted data to /tmp/clive/ for other subtasks.
COMPLETION: Use <cmd type="task_complete">summary</cmd> when goal is achieved.
```

**Step 2: Verify it loads**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -c "from prompts import load_driver; d = load_driver('browser'); print(f'Loaded {len(d)} chars'); assert 'LYNX PATTERNS' in d"`
Expected: `Loaded NNN chars` (no assertion error)

**Step 3: Commit**

```bash
git add drivers/browser.md
git commit -m "feat: add browser driver prompt (lynx/curl/wget reference card)"
```

---

### Task 7: Eval harness — session fixture

Build `session_fixture.py` — creates isolated tmux sessions with known filesystem state for reproducible evals.

**Files:**
- Create: `evals/` directory structure
- Create: `evals/__init__.py`
- Create: `evals/harness/__init__.py`
- Create: `evals/harness/session_fixture.py`
- Create: `tests/test_session_fixture.py`

**Step 1: Create directory structure**

```bash
mkdir -p evals/harness evals/layer2/shell/fixtures evals/layer2/lynx/fixtures
touch evals/__init__.py evals/harness/__init__.py
```

**Step 2: Write the failing test**

Create `tests/test_session_fixture.py`:

```python
"""Tests for eval session fixture management."""
import os
import pytest
from evals.harness.session_fixture import EvalFixture


@pytest.fixture
def fixture_dir(tmp_path):
    """Create a minimal fixture directory."""
    # Create some fixture files
    (tmp_path / "file1.txt").write_text("hello world\n")
    (tmp_path / "file2.txt").write_text("second file\n")
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested content\n")
    return tmp_path


def test_fixture_creates_workdir(fixture_dir):
    with EvalFixture(fixture_dir=str(fixture_dir)) as ef:
        assert os.path.isdir(ef.workdir)
        assert os.path.exists(os.path.join(ef.workdir, "file1.txt"))
        assert os.path.exists(os.path.join(ef.workdir, "subdir", "nested.txt"))


def test_fixture_creates_tmux_session(fixture_dir):
    with EvalFixture(fixture_dir=str(fixture_dir)) as ef:
        assert ef.session_name.startswith("clive_eval_")
        # Verify session exists in tmux
        import libtmux
        server = libtmux.Server()
        session = server.sessions.filter(session_name=ef.session_name)
        assert len(session) == 1


def test_fixture_cleanup(fixture_dir):
    session_name = None
    workdir = None
    with EvalFixture(fixture_dir=str(fixture_dir)) as ef:
        session_name = ef.session_name
        workdir = ef.workdir
    # After exit, session and workdir should be gone
    import libtmux
    server = libtmux.Server()
    sessions = server.sessions.filter(session_name=session_name)
    assert len(sessions) == 0
    assert not os.path.exists(workdir)
```

**Step 3: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_session_fixture.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 4: Implement session_fixture.py**

Create `evals/harness/session_fixture.py`:

```python
"""Eval session fixture: isolated tmux + filesystem for reproducible evals.

Creates a fresh tmux session with a known filesystem state, runs the eval,
and tears everything down. Each eval gets its own session name and temp
directory to avoid interference.
"""
import os
import shutil
import tempfile
import time
import uuid

import libtmux

from models import PaneInfo


class EvalFixture:
    """Context manager for eval sessions.

    Usage:
        with EvalFixture(fixture_dir="evals/layer2/shell/fixtures/task_001") as ef:
            ef.send_keys("ls -la")
            screen = ef.capture()
            assert "file1.txt" in screen
    """

    def __init__(
        self,
        fixture_dir: str | None = None,
        pane_app_type: str = "shell",
        session_prefix: str = "clive_eval",
    ):
        self.fixture_dir = fixture_dir
        self.pane_app_type = pane_app_type
        self.session_name = f"{session_prefix}_{uuid.uuid4().hex[:8]}"
        self.workdir: str = ""
        self.session: libtmux.Session | None = None
        self.pane: libtmux.Pane | None = None
        self.pane_info: PaneInfo | None = None

    def __enter__(self):
        # Create isolated workdir
        self.workdir = tempfile.mkdtemp(prefix="clive_eval_")

        # Copy fixture files if provided
        if self.fixture_dir and os.path.isdir(self.fixture_dir):
            for item in os.listdir(self.fixture_dir):
                src = os.path.join(self.fixture_dir, item)
                dst = os.path.join(self.workdir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

        # Create tmux session
        server = libtmux.Server()
        self.session = server.new_session(
            session_name=self.session_name,
            kill_session=True,
            attach=False,
        )
        self.pane = self.session.active_window.active_pane

        # Set up shell environment
        self.pane.send_keys('export PS1="[AGENT_READY] $ "', enter=True)
        self.pane.send_keys(f'cd {self.workdir}', enter=True)
        time.sleep(0.5)

        self.pane_info = PaneInfo(
            pane=self.pane,
            app_type=self.pane_app_type,
            description=f"Eval pane ({self.pane_app_type})",
            name="eval",
            idle_timeout=2.0,
        )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Kill tmux session
        if self.session:
            try:
                self.session.kill()
            except Exception:
                pass

        # Remove workdir
        if self.workdir and os.path.exists(self.workdir):
            shutil.rmtree(self.workdir, ignore_errors=True)

        return False

    def send_keys(self, keys: str, enter: bool = True):
        """Send keys to the eval pane."""
        self.pane.send_keys(keys, enter=enter)

    def capture(self) -> str:
        """Capture current screen content."""
        lines = self.pane.cmd("capture-pane", "-p").stdout
        return "\n".join(lines) if lines else ""

    def wait_for_prompt(self, timeout: float = 5.0) -> str:
        """Wait for [AGENT_READY] prompt to appear, return screen."""
        start = time.time()
        while time.time() - start < timeout:
            screen = self.capture()
            lines = screen.strip().split("\n")
            if lines and "[AGENT_READY] $" in lines[-1]:
                return screen
            time.sleep(0.1)
        return self.capture()  # return whatever we have
```

**Step 5: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_session_fixture.py -v`
Expected: 3 PASSED (requires tmux server running)

**Step 6: Commit**

```bash
git add evals/ tests/test_session_fixture.py
git commit -m "feat: add eval session fixture with isolated tmux + filesystem"
```

---

### Task 8: Eval harness — verifier

Build `verifier.py` — runs deterministic shell checks or cached LLM verification against eval results.

**Files:**
- Create: `evals/harness/verifier.py`
- Create: `tests/test_verifier.py`

**Step 1: Write the failing test**

Create `tests/test_verifier.py`:

```python
"""Tests for eval verifiers."""
import os
import json
import tempfile
from evals.harness.verifier import DeterministicVerifier, verify_task


def test_deterministic_verifier_pass(tmp_path):
    result_file = tmp_path / "result.txt"
    result_file.write_text("hello world\n")
    v = DeterministicVerifier(
        check=f'grep -q "hello" {result_file}',
        workdir=str(tmp_path),
    )
    assert v.verify() is True


def test_deterministic_verifier_fail(tmp_path):
    result_file = tmp_path / "result.txt"
    result_file.write_text("goodbye\n")
    v = DeterministicVerifier(
        check=f'grep -q "hello" {result_file}',
        workdir=str(tmp_path),
    )
    assert v.verify() is False


def test_verify_task_deterministic(tmp_path):
    result_file = tmp_path / "output.txt"
    result_file.write_text("42\n")
    task_def = {
        "success_criteria": {
            "type": "deterministic",
            "check": f'test "$(cat {result_file})" = "42"',
        }
    }
    passed, detail = verify_task(task_def, workdir=str(tmp_path))
    assert passed is True
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_verifier.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Implement verifier.py**

Create `evals/harness/verifier.py`:

```python
"""Eval verifiers: deterministic (shell) and LLM-based with caching.

Deterministic verifiers run a shell command and check exit code.
LLM verifiers send the task + result to an LLM and cache the verdict.
"""
import hashlib
import json
import os
import subprocess


class DeterministicVerifier:
    """Verify eval results via shell command exit code."""

    def __init__(self, check: str, workdir: str):
        self.check = check
        self.workdir = workdir

    def verify(self) -> bool:
        """Run the check command. Returns True if exit code is 0."""
        try:
            result = subprocess.run(
                self.check,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


class LLMVerifier:
    """Verify eval results via LLM judgment, with caching.

    Cache key is hash of (task_description, agent_output, verify_prompt).
    Cached verdicts are stored as JSON in the cache directory.
    """

    def __init__(
        self,
        verify_prompt: str,
        cache_dir: str = ".eval_cache",
    ):
        self.verify_prompt = verify_prompt
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_key(self, task: str, output: str) -> str:
        content = f"{task}|{output}|{self.verify_prompt}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _get_cached(self, key: str) -> dict | None:
        path = os.path.join(self.cache_dir, f"{key}.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return None

    def _set_cached(self, key: str, result: dict):
        path = os.path.join(self.cache_dir, f"{key}.json")
        with open(path, "w") as f:
            json.dump(result, f)

    def verify(self, task_description: str, agent_output: str) -> tuple[bool, str]:
        """Verify via LLM. Returns (passed, reasoning).

        Uses cache if available. Otherwise calls LLM and caches result.
        """
        key = self._cache_key(task_description, agent_output)
        cached = self._get_cached(key)
        if cached:
            return cached["passed"], cached.get("reasoning", "cached")

        # LLM verification
        from llm import get_client, chat

        client = get_client()
        messages = [
            {"role": "system", "content": self.verify_prompt},
            {
                "role": "user",
                "content": (
                    f"Task: {task_description}\n\n"
                    f"Agent output:\n{agent_output}\n\n"
                    "Did the agent successfully complete the task? "
                    "Respond with JSON: {\"passed\": true/false, \"reasoning\": \"...\"}"
                ),
            },
        ]
        response, _, _ = chat(client, messages)

        try:
            verdict = json.loads(response)
            passed = verdict.get("passed", False)
            reasoning = verdict.get("reasoning", "")
        except json.JSONDecodeError:
            passed = "passed" in response.lower() and "true" in response.lower()
            reasoning = response

        result = {"passed": passed, "reasoning": reasoning}
        self._set_cached(key, result)
        return passed, reasoning


def verify_task(
    task_def: dict,
    workdir: str,
    agent_output: str = "",
    cache_dir: str = ".eval_cache",
) -> tuple[bool, str]:
    """Verify a task result based on its success_criteria definition.

    Returns (passed, detail_string).
    """
    criteria = task_def["success_criteria"]

    if criteria["type"] == "deterministic":
        v = DeterministicVerifier(check=criteria["check"], workdir=workdir)
        passed = v.verify()
        return passed, "deterministic check " + ("passed" if passed else "failed")

    elif criteria["type"] == "llm":
        prompt_path = criteria.get("prompt", "")
        if os.path.exists(prompt_path):
            with open(prompt_path) as f:
                verify_prompt = f.read()
        else:
            verify_prompt = (
                "You are an eval verifier. Determine if the agent completed "
                "the task successfully based on the output provided."
            )
        v = LLMVerifier(verify_prompt=verify_prompt, cache_dir=cache_dir)
        return v.verify(task_def.get("task", ""), agent_output)

    else:
        return False, f"Unknown criteria type: {criteria['type']}"
```

**Step 4: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_verifier.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
git add evals/harness/verifier.py tests/test_verifier.py
git commit -m "feat: add eval verifiers (deterministic + cached LLM)"
```

---

### Task 9: Eval harness — metrics and runner

Build `metrics.py` for collecting eval results and `run_eval.py` as the CLI entry point.

**Files:**
- Create: `evals/harness/metrics.py`
- Create: `evals/harness/run_eval.py`
- Create: `evals/harness/pricing.json`
- Create: `tests/test_metrics.py`

**Step 1: Write the failing test**

Create `tests/test_metrics.py`:

```python
"""Tests for eval metrics collection."""
from evals.harness.metrics import EvalResult, EvalReport


def test_eval_result_creation():
    r = EvalResult(
        task_id="shell_001",
        layer=2,
        tool="shell",
        passed=True,
        turns_used=3,
        min_turns=2,
        prompt_tokens=500,
        completion_tokens=200,
        elapsed_seconds=4.5,
        detail="deterministic check passed",
    )
    assert r.passed is True
    assert r.turn_efficiency == 2 / 3


def test_eval_report_summary():
    results = [
        EvalResult("s1", 2, "shell", True, 3, 2, 500, 200, 4.5, "ok"),
        EvalResult("s2", 2, "shell", False, 8, 3, 800, 400, 12.0, "fail"),
        EvalResult("s3", 2, "shell", True, 2, 2, 300, 100, 2.0, "ok"),
    ]
    report = EvalReport(results)
    assert report.completion_rate == 2 / 3
    assert report.total_tasks == 3
    assert report.total_tokens == (500 + 200 + 800 + 400 + 300 + 100)
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Implement metrics.py**

Create `evals/harness/metrics.py`:

```python
"""Eval metrics collection and reporting."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalResult:
    """Result of a single eval task."""
    task_id: str
    layer: int
    tool: str
    passed: bool
    turns_used: int
    min_turns: int
    prompt_tokens: int
    completion_tokens: int
    elapsed_seconds: float
    detail: str
    error_recovered: bool = False
    false_completion: bool = False

    @property
    def turn_efficiency(self) -> float:
        """Ratio of min_turns / turns_used. 1.0 = optimal."""
        if self.turns_used == 0:
            return 0.0
        return self.min_turns / self.turns_used

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class EvalReport:
    """Aggregated eval report."""
    results: list[EvalResult]

    @property
    def total_tasks(self) -> int:
        return len(self.results)

    @property
    def passed_tasks(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def completion_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.passed_tasks / self.total_tasks

    @property
    def avg_turn_efficiency(self) -> float:
        efficiencies = [r.turn_efficiency for r in self.results if r.turns_used > 0]
        if not efficiencies:
            return 0.0
        return sum(efficiencies) / len(efficiencies)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.results)

    @property
    def total_elapsed(self) -> float:
        return sum(r.elapsed_seconds for r in self.results)

    @property
    def error_recovery_rate(self) -> float:
        """Of tasks that encountered errors, how many recovered?"""
        errored = [r for r in self.results if r.error_recovered or not r.passed]
        if not errored:
            return 1.0
        return sum(1 for r in errored if r.error_recovered) / len(errored)

    @property
    def false_completion_rate(self) -> float:
        """How often did the agent claim success but fail verification?"""
        completed = [r for r in self.results if r.turns_used > 0]
        if not completed:
            return 0.0
        return sum(1 for r in completed if r.false_completion) / len(completed)

    def to_dict(self) -> dict:
        return {
            "total_tasks": self.total_tasks,
            "passed": self.passed_tasks,
            "completion_rate": round(self.completion_rate, 3),
            "avg_turn_efficiency": round(self.avg_turn_efficiency, 3),
            "total_tokens": self.total_tokens,
            "total_elapsed_seconds": round(self.total_elapsed, 1),
            "error_recovery_rate": round(self.error_recovery_rate, 3),
            "false_completion_rate": round(self.false_completion_rate, 3),
            "results": [
                {
                    "task_id": r.task_id,
                    "passed": r.passed,
                    "turns": r.turns_used,
                    "tokens": r.total_tokens,
                    "elapsed": round(r.elapsed_seconds, 1),
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }

    def print_summary(self):
        """Print a human-readable summary."""
        from output import progress
        progress(f"\n{'=' * 60}")
        progress(f"EVAL RESULTS: {self.passed_tasks}/{self.total_tasks} passed "
                 f"({self.completion_rate:.0%})")
        progress(f"{'=' * 60}")
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            progress(f"  [{status}] {r.task_id} "
                     f"(turns: {r.turns_used}, tokens: {r.total_tokens})")
            if not r.passed:
                progress(f"         {r.detail}")
        progress(f"{'~' * 60}")
        progress(f"Turn efficiency: {self.avg_turn_efficiency:.0%}")
        progress(f"Total tokens:    {self.total_tokens:,}")
        progress(f"Total time:      {self.total_elapsed:.1f}s")
        progress(f"{'=' * 60}\n")
```

**Step 4: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_metrics.py -v`
Expected: 2 PASSED

**Step 5: Create pricing.json**

Create `evals/harness/pricing.json`:

```json
{
  "claude-sonnet-4-20250514": {"prompt_per_1k": 0.003, "completion_per_1k": 0.015},
  "claude-opus-4-20250514": {"prompt_per_1k": 0.015, "completion_per_1k": 0.075},
  "gpt-4o": {"prompt_per_1k": 0.005, "completion_per_1k": 0.015},
  "gemini-2.0-flash": {"prompt_per_1k": 0.0001, "completion_per_1k": 0.0004},
  "default": {"prompt_per_1k": 0.003, "completion_per_1k": 0.015}
}
```

**Step 6: Create run_eval.py**

Create `evals/harness/run_eval.py`:

```python
#!/usr/bin/env python3
"""Eval runner for clive.

Usage:
    python evals/harness/run_eval.py --layer 2 --tool shell
    python evals/harness/run_eval.py --layer 2
    python evals/harness/run_eval.py --all
    python evals/harness/run_eval.py --layer 2 --tool shell --driver drivers/shell_v2.md
"""
import argparse
import json
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evals.harness.session_fixture import EvalFixture
from evals.harness.verifier import verify_task
from evals.harness.metrics import EvalResult, EvalReport
from executor import run_subtask
from models import Subtask, PaneInfo
from llm import get_client


def load_tasks(layer: int, tool: str | None = None) -> list[dict]:
    """Load task definitions for a layer (and optionally a specific tool)."""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if tool:
        tasks_path = os.path.join(base, f"layer{layer}", tool, "tasks.json")
        if os.path.exists(tasks_path):
            with open(tasks_path) as f:
                return json.load(f)
        return []

    # Load all tools for this layer
    layer_dir = os.path.join(base, f"layer{layer}")
    if not os.path.isdir(layer_dir):
        return []

    all_tasks = []
    for tool_name in sorted(os.listdir(layer_dir)):
        tasks_path = os.path.join(layer_dir, tool_name, "tasks.json")
        if os.path.exists(tasks_path):
            with open(tasks_path) as f:
                all_tasks.extend(json.load(f))
    return all_tasks


def run_single_task(
    task_def: dict,
    driver_override: str | None = None,
) -> EvalResult:
    """Run a single eval task and return the result."""
    task_id = task_def["id"]
    tool = task_def.get("tool", "shell")
    layer = task_def.get("layer", 2)
    mode = task_def.get("mode", "interactive")
    max_turns = task_def.get("max_turns", 8)

    # Resolve fixture directory
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    fixture_dir = None
    if "initial_state" in task_def and "filesystem" in task_def["initial_state"]:
        fixture_dir = os.path.join(base, f"layer{layer}", tool,
                                   task_def["initial_state"]["filesystem"])

    start_time = time.time()

    with EvalFixture(fixture_dir=fixture_dir, pane_app_type=tool) as ef:
        # Optionally override driver prompt
        if driver_override:
            os.environ["CLIVE_EVAL_DRIVER_OVERRIDE"] = driver_override

        # Create subtask for the worker
        subtask = Subtask(
            id=task_id,
            description=task_def["task"],
            pane="eval",
            max_turns=max_turns,
        )

        try:
            result = run_subtask(
                subtask=subtask,
                pane_info=ef.pane_info,
                dep_context="",
            )

            elapsed = time.time() - start_time
            screen = ef.capture()

            # Verify
            passed, detail = verify_task(
                task_def,
                workdir=ef.workdir,
                agent_output=screen,
            )

            return EvalResult(
                task_id=task_id,
                layer=layer,
                tool=tool,
                passed=passed,
                turns_used=result.turns_used,
                min_turns=task_def.get("min_turns", 1),
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                elapsed_seconds=elapsed,
                detail=detail,
                false_completion=(
                    result.status.value == "completed" and not passed
                ),
            )
        except Exception as e:
            elapsed = time.time() - start_time
            return EvalResult(
                task_id=task_id,
                layer=layer,
                tool=tool,
                passed=False,
                turns_used=0,
                min_turns=task_def.get("min_turns", 1),
                prompt_tokens=0,
                completion_tokens=0,
                elapsed_seconds=elapsed,
                detail=f"Exception: {e}",
            )
        finally:
            if "CLIVE_EVAL_DRIVER_OVERRIDE" in os.environ:
                del os.environ["CLIVE_EVAL_DRIVER_OVERRIDE"]


def main():
    parser = argparse.ArgumentParser(description="clive eval runner")
    parser.add_argument("--layer", type=int, help="Layer to eval (2, 3, 4, 1)")
    parser.add_argument("--tool", help="Specific tool (e.g., shell, lynx)")
    parser.add_argument("--all", action="store_true", help="Run all evals")
    parser.add_argument("--driver", action="append", help="Driver prompt override(s)")
    parser.add_argument("--output", help="Save JSON report to file")
    parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 on any failure")
    parser.add_argument("--baseline", help="Baseline JSON for regression comparison")
    args = parser.parse_args()

    if not args.layer and not args.all:
        parser.error("Specify --layer N or --all")

    # Load tasks
    if args.all:
        tasks = []
        for layer in [2, 3, 4, 1]:
            tasks.extend(load_tasks(layer))
    else:
        tasks = load_tasks(args.layer, args.tool)

    if not tasks:
        print("No tasks found.", file=sys.stderr)
        sys.exit(1)

    print(f"Running {len(tasks)} eval tasks...", file=sys.stderr)

    # Run evals
    results = []
    for task_def in tasks:
        driver = args.driver[0] if args.driver else None
        print(f"  [{task_def['id']}] {task_def['task'][:60]}...", file=sys.stderr)
        result = run_single_task(task_def, driver_override=driver)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.detail}", file=sys.stderr)

    # Report
    report = EvalReport(results)
    report.print_summary()

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report.to_dict(), f, indent=2)
        print(f"Report saved to {args.output}", file=sys.stderr)

    if args.ci and report.completion_rate < 1.0:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

**Step 7: Run tests**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_metrics.py -v`
Expected: 2 PASSED

**Step 8: Commit**

```bash
git add evals/harness/metrics.py evals/harness/run_eval.py evals/harness/pricing.json tests/test_metrics.py
git commit -m "feat: add eval metrics, report, and runner CLI"
```

---

### Task 10: Shell eval tasks

Write 5 eval tasks for the shell driver with fixtures and deterministic verification.

**Files:**
- Create: `evals/layer2/shell/tasks.json`
- Create: `evals/layer2/shell/fixtures/find_txt_files/` (fixture dirs)
- Create: `evals/layer2/shell/fixtures/count_pattern/`
- Create: `evals/layer2/shell/fixtures/pipeline_test/`

**Step 1: Create fixture directories with test data**

```bash
# Task 1: find .txt files
mkdir -p evals/layer2/shell/fixtures/find_txt_files
echo "hello" > evals/layer2/shell/fixtures/find_txt_files/notes.txt
echo "world" > evals/layer2/shell/fixtures/find_txt_files/readme.txt
echo "data" > evals/layer2/shell/fixtures/find_txt_files/data.csv
mkdir -p evals/layer2/shell/fixtures/find_txt_files/subdir
echo "nested" > evals/layer2/shell/fixtures/find_txt_files/subdir/deep.txt

# Task 2: count pattern matches
mkdir -p evals/layer2/shell/fixtures/count_pattern
printf 'line1 TODO fix this\nline2 ok\nline3 TODO also this\n' > evals/layer2/shell/fixtures/count_pattern/code.py
printf 'no matches here\njust normal code\n' > evals/layer2/shell/fixtures/count_pattern/other.py
printf 'TODO third one\n' > evals/layer2/shell/fixtures/count_pattern/notes.txt

# Task 3: pipeline test (word frequency)
mkdir -p evals/layer2/shell/fixtures/pipeline_test
printf 'the cat sat on the mat\nthe cat sat\nthe mat\n' > evals/layer2/shell/fixtures/pipeline_test/input.txt
```

**Step 2: Write tasks.json**

Create `evals/layer2/shell/tasks.json`:

```json
[
  {
    "id": "shell_find_txt_001",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Find all .txt files in the current directory (recursively) and write their paths, one per line, to /tmp/clive/result.txt. Use relative paths.",
    "initial_state": {
      "filesystem": "fixtures/find_txt_files/"
    },
    "success_criteria": {
      "type": "deterministic",
      "check": "sort /tmp/clive/result.txt | grep -c '.txt$' | grep -q '3'"
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 30
  },
  {
    "id": "shell_count_pattern_002",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Count the total number of lines containing 'TODO' across all files in the current directory (recursively). Write just the number to /tmp/clive/result.txt.",
    "initial_state": {
      "filesystem": "fixtures/count_pattern/"
    },
    "success_criteria": {
      "type": "deterministic",
      "check": "test \"$(cat /tmp/clive/result.txt | tr -d '[:space:]')\" = \"3\""
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 30
  },
  {
    "id": "shell_pipeline_003",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Using the file input.txt, count word frequencies and write the top 3 most common words (one per line, format: 'COUNT WORD') to /tmp/clive/result.txt, sorted by frequency descending.",
    "initial_state": {
      "filesystem": "fixtures/pipeline_test/"
    },
    "success_criteria": {
      "type": "deterministic",
      "check": "head -1 /tmp/clive/result.txt | grep -q 'the'"
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 30
  },
  {
    "id": "shell_disk_usage_004",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "List all files in the current directory with their sizes in human-readable format (like ls -lh), and write the total count of files (not directories) to /tmp/clive/result.txt as a single number.",
    "initial_state": {
      "filesystem": "fixtures/find_txt_files/"
    },
    "success_criteria": {
      "type": "deterministic",
      "check": "test \"$(cat /tmp/clive/result.txt | tr -d '[:space:]')\" -ge 3"
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 30
  },
  {
    "id": "shell_json_extract_005",
    "layer": 2,
    "tool": "shell",
    "mode": "interactive",
    "task": "Create a JSON file at /tmp/clive/data.json containing: [{\"name\":\"alice\",\"age\":30},{\"name\":\"bob\",\"age\":25},{\"name\":\"charlie\",\"age\":35}]. Then extract all names and write them to /tmp/clive/result.txt, one per line.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "grep -q 'alice' /tmp/clive/result.txt && grep -q 'bob' /tmp/clive/result.txt && grep -q 'charlie' /tmp/clive/result.txt"
    },
    "min_turns": 2,
    "max_turns": 10,
    "timeout_seconds": 45
  }
]
```

**Step 3: Verify tasks load**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -c "from evals.harness.run_eval import load_tasks; t = load_tasks(2, 'shell'); print(f'{len(t)} tasks loaded'); assert len(t) == 5"`
Expected: `5 tasks loaded`

**Step 4: Commit**

```bash
git add evals/layer2/shell/
git commit -m "feat: add 5 shell eval tasks with fixtures"
```

---

### Task 11: Lynx/browser eval tasks

Write 5 eval tasks for the browser driver. These use curl/lynx against known public endpoints for reproducibility.

**Files:**
- Create: `evals/layer2/lynx/tasks.json`

**Step 1: Write tasks.json**

Create `evals/layer2/lynx/tasks.json`:

```json
[
  {
    "id": "lynx_fetch_page_001",
    "layer": 2,
    "tool": "browser",
    "mode": "interactive",
    "task": "Fetch the page at http://example.com using lynx -dump and write the main heading (the h1 text) to /tmp/clive/result.txt.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "grep -qi 'example domain' /tmp/clive/result.txt"
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 30
  },
  {
    "id": "lynx_extract_links_002",
    "layer": 2,
    "tool": "browser",
    "mode": "interactive",
    "task": "Extract all links from http://example.com and write them to /tmp/clive/result.txt, one URL per line.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "grep -q 'iana.org' /tmp/clive/result.txt"
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 30
  },
  {
    "id": "lynx_json_api_003",
    "layer": 2,
    "tool": "browser",
    "mode": "interactive",
    "task": "Fetch https://jsonplaceholder.typicode.com/posts/1 using curl, extract the 'title' field, and write it to /tmp/clive/result.txt.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "grep -qi 'provident' /tmp/clive/result.txt || grep -qi 'sunt aut' /tmp/clive/result.txt"
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 30
  },
  {
    "id": "lynx_headers_check_004",
    "layer": 2,
    "tool": "browser",
    "mode": "interactive",
    "task": "Check the HTTP headers of http://example.com using curl -sI. Write the Content-Type header value to /tmp/clive/result.txt.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "grep -qi 'text/html' /tmp/clive/result.txt"
    },
    "min_turns": 2,
    "max_turns": 8,
    "timeout_seconds": 30
  },
  {
    "id": "lynx_multi_api_005",
    "layer": 2,
    "tool": "browser",
    "mode": "interactive",
    "task": "Fetch the first 3 posts from https://jsonplaceholder.typicode.com/posts (use ?_limit=3), extract their titles, and write them to /tmp/clive/result.txt, one per line.",
    "initial_state": {},
    "success_criteria": {
      "type": "deterministic",
      "check": "test $(wc -l < /tmp/clive/result.txt) -ge 3"
    },
    "min_turns": 2,
    "max_turns": 10,
    "timeout_seconds": 45
  }
]
```

**Step 2: Verify tasks load**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -c "from evals.harness.run_eval import load_tasks; t = load_tasks(2, 'lynx'); print(f'{len(t)} tasks loaded'); assert len(t) == 5"`
Expected: `5 tasks loaded`

**Step 3: Commit**

```bash
git add evals/layer2/lynx/
git commit -m "feat: add 5 browser eval tasks (lynx/curl against public APIs)"
```

---

### Task 12: Wire eval pane lock for runner

The eval runner uses `run_subtask()` from executor.py which expects a pane lock in `_pane_locks`. The eval fixture needs to register this lock. Also ensure `/tmp/clive/` exists in the eval session.

**Files:**
- Modify: `evals/harness/session_fixture.py` (add pane lock setup)
- Modify: `evals/harness/run_eval.py` (ensure /tmp/clive/ creation)

**Step 1: Update EvalFixture.__enter__ to register pane lock**

In `session_fixture.py`, add to `__enter__` after creating `self.pane_info`:

```python
        # Register pane lock for executor compatibility
        from executor import _pane_locks
        _pane_locks["eval"] = __import__("threading").Lock()

        # Ensure shared working dir exists
        self.pane.send_keys("mkdir -p /tmp/clive", enter=True)
        time.sleep(0.3)
```

**Step 2: Verify tests still pass**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_session_fixture.py tests/test_verifier.py tests/test_metrics.py tests/test_output.py tests/test_drivers.py -v`
Expected: All PASSED

**Step 3: Commit**

```bash
git add evals/harness/session_fixture.py evals/harness/run_eval.py
git commit -m "fix: register eval pane lock and create /tmp/clive in fixture"
```

---

### Task 13: Integration smoke test

Run one shell eval task end-to-end to verify the full pipeline works: fixture setup → worker execution → verification → metrics.

**Files:** None (this is a verification step)

**Step 1: Run a single shell eval**

Run: `cd /Users/martintreiber/Documents/Development/clive && python evals/harness/run_eval.py --layer 2 --tool shell 2>&1 | head -40`

Expected: Output showing task execution attempts and a summary report. Some tasks may fail (the LLM needs to be configured) — the important thing is that the harness runs without crashing.

**Step 2: If the harness crashes, debug and fix**

Common issues:
- Missing `_pane_locks` registration → fixed in Task 12
- `/tmp/clive` not created → fixed in Task 12
- LLM not configured → set `LLM_PROVIDER` and API key in `.env`
- tmux not running → start tmux server first

**Step 3: Run unit tests to confirm nothing broke**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/ -v`
Expected: All PASSED

**Step 4: Final commit if any fixes were needed**

```bash
git add -A
git commit -m "fix: integration fixes from eval smoke test"
```

---

## Summary

After completing all 13 tasks:

| Component | Files | Purpose |
|---|---|---|
| Output routing | `output.py` | `--quiet` mode, telemetry/result separation |
| Driver auto-discovery | `prompts.py`, `drivers/*.md` | Tool-specific worker prompts |
| Shell driver | `drivers/shell.md` | Bash reference card for shell workers |
| Browser driver | `drivers/browser.md` | lynx/curl reference card for browser workers |
| Eval fixture | `evals/harness/session_fixture.py` | Isolated tmux + filesystem per eval |
| Verifier | `evals/harness/verifier.py` | Deterministic + cached LLM verification |
| Metrics | `evals/harness/metrics.py` | Result collection and reporting |
| Runner | `evals/harness/run_eval.py` | CLI entry point for evals |
| Shell evals | `evals/layer2/shell/` | 5 tasks with fixtures |
| Browser evals | `evals/layer2/lynx/` | 5 tasks against public endpoints |

**Next step after Phase 1:** Iterate driver prompts against eval results. Run evals, identify failure patterns, improve drivers, re-run. This is the feedback loop that makes specialization valuable. Then proceed to Phase 2 (execution mode formalization).
