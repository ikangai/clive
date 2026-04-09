# Agent Addressing & Peer Conversation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement `clive@host` addressing with peer conversation protocol, BYOLLM via SSH env forwarding, and auto-detection of conversational vs TUI mode.

**Architecture:** New `agents.py` handles address parsing and resolution. The planner extracts `clive@host` from task text before routing. `session.py` gets lazy pane injection. `executor.py` gains turn-state-aware handling for agent panes. `output.py` gets a conversational output skin. `clive.py` adds `--conversational` flag with `isatty()` auto-detection. Inner Clive reads tasks from stdin and emits structured `TURN:` protocol.

**Tech Stack:** Python 3, SSH, tmux (libtmux), PyYAML, existing remote.py protocol foundation

**Design doc:** `docs/plans/2026-04-08-agent-addressing-design.md`

---

### Task 1: Address parsing and agent resolution (`agents.py`)

**Files:**
- Create: `agents.py`
- Create: `tests/test_agents.py`

**Step 1: Write the failing tests**

```python
# tests/test_agents.py
"""Tests for agent addressing and resolution."""
import os
import tempfile
from agents import parse_agent_addresses, resolve_agent, build_agent_ssh_cmd


# ─── Address parsing ─────────────────────────────────────────────────────────

def test_parse_single_address():
    result = parse_agent_addresses("ask clive@devbox to check disk usage")
    assert len(result) == 1
    assert result[0] == ("devbox", "ask to check disk usage")


def test_parse_address_at_start():
    result = parse_agent_addresses("clive@localhost read HN")
    assert result[0] == ("localhost", "read HN")


def test_parse_no_address():
    result = parse_agent_addresses("check disk usage")
    assert result == []


def test_parse_multiple_addresses():
    result = parse_agent_addresses(
        "ask clive@gpu to render video then clive@web to upload it"
    )
    assert len(result) == 2
    hosts = [r[0] for r in result]
    assert "gpu" in hosts
    assert "web" in hosts


def test_parse_address_with_dots():
    result = parse_agent_addresses("clive@prod.example.com check health")
    assert result[0][0] == "prod.example.com"


def test_parse_address_with_hyphens():
    result = parse_agent_addresses("clive@my-server check health")
    assert result[0][0] == "my-server"


# ─── Resolution ──────────────────────────────────────────────────────────────

def test_resolve_auto():
    """Auto-resolve without registry returns default SSH pane def."""
    pane_def = resolve_agent("myhost")
    assert pane_def["name"] == "agent-myhost"
    assert pane_def["app_type"] == "agent"
    assert pane_def["host"] == "myhost"
    assert "ssh" in pane_def["cmd"]
    assert "myhost" in pane_def["cmd"]
    assert "--conversational" in pane_def["cmd"]


def test_resolve_from_registry():
    """Registry entry overrides auto-resolve."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("devbox:\n  host: devbox.local\n  toolset: web\n  path: /opt/clive/clive.py\n")
        f.flush()
        try:
            pane_def = resolve_agent("devbox", registry_path=f.name)
            assert pane_def["host"] == "devbox.local"
            assert "-t web" in pane_def["cmd"]
            assert "/opt/clive/clive.py" in pane_def["cmd"]
        finally:
            os.unlink(f.name)


def test_resolve_registry_with_key():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("secure:\n  key: ~/.ssh/special_key\n")
        f.flush()
        try:
            pane_def = resolve_agent("secure", registry_path=f.name)
            assert "-i ~/.ssh/special_key" in pane_def["cmd"]
        finally:
            os.unlink(f.name)


def test_resolve_registry_missing_host_defaults_to_name():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("localhost:\n  toolset: web\n")
        f.flush()
        try:
            pane_def = resolve_agent("localhost", registry_path=f.name)
            assert pane_def["host"] == "localhost"
        finally:
            os.unlink(f.name)


# ─── SSH command building ────────────────────────────────────────────────────

def test_build_ssh_cmd_basic():
    cmd = build_agent_ssh_cmd("myhost", {})
    assert cmd.startswith("ssh ")
    assert "myhost" in cmd
    assert "-t" not in cmd  # no TTY allocation
    assert "--conversational" in cmd


def test_build_ssh_cmd_with_key():
    cmd = build_agent_ssh_cmd("myhost", {"key": "~/.ssh/mykey"})
    assert "-i ~/.ssh/mykey" in cmd


def test_build_ssh_cmd_with_toolset():
    cmd = build_agent_ssh_cmd("myhost", {"toolset": "web"})
    assert "-t web" in cmd


def test_build_ssh_cmd_with_custom_path():
    cmd = build_agent_ssh_cmd("myhost", {"path": "/opt/clive/clive.py"})
    assert "/opt/clive/clive.py" in cmd


def test_build_ssh_cmd_forwards_env():
    """SSH command should include SendEnv for API keys."""
    # Set a test env var to verify it gets forwarded
    old = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        cmd = build_agent_ssh_cmd("myhost", {})
        assert "SendEnv=ANTHROPIC_API_KEY" in cmd
    finally:
        if old:
            os.environ["ANTHROPIC_API_KEY"] = old
        else:
            del os.environ["ANTHROPIC_API_KEY"]
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agents.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agents'`

**Step 3: Implement `agents.py`**

```python
# agents.py
"""Agent addressing and resolution for clive@host communication.

Parses clive@host addresses from task text, resolves them via a YAML
registry (~/.clive/agents.yaml) or auto-resolve fallback, and builds
SSH commands with API key forwarding (BYOLLM).

Address format: clive@<host> where host is [\w.\-]+
Registry: ~/.clive/agents.yaml (optional)
SSH: no -t flag (no TTY) → inner clive auto-detects conversational mode
"""
import os
import re

DEFAULT_REGISTRY = os.path.expanduser("~/.clive/agents.yaml")
DEFAULT_CLIVE_PATH = "python3 clive.py"

# Env vars to forward via SSH SendEnv (BYOLLM)
_FORWARD_ENVS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "LLM_PROVIDER",
    "AGENT_MODEL",
]

_ADDR_RE = re.compile(r"clive@([\w.\-]+)")


def parse_agent_addresses(task: str) -> list[tuple[str, str]]:
    """Extract clive@host addresses from task text.

    Returns list of (host, remaining_task) tuples.
    The clive@host token is stripped from the remaining task.
    """
    matches = list(_ADDR_RE.finditer(task))
    if not matches:
        return []

    results = []
    for match in matches:
        host = match.group(1)
        remaining = task[:match.start()] + task[match.end():]
        remaining = re.sub(r"\s+", " ", remaining).strip()
        results.append((host, remaining))

    return results


def _load_registry(path: str | None = None) -> dict:
    """Load agents.yaml registry. Returns empty dict if not found."""
    path = path or DEFAULT_REGISTRY
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_agent(host: str, registry_path: str | None = None) -> dict:
    """Resolve a clive@host address to a pane definition dict.

    Checks registry first, falls back to auto-resolve.
    Returns dict compatible with PANES entries in toolsets.py.
    """
    registry = _load_registry(registry_path)
    config = registry.get(host, {})

    actual_host = config.get("host", host)
    cmd = build_agent_ssh_cmd(actual_host, config)

    return {
        "name": f"agent-{host}",
        "cmd": cmd,
        "app_type": "agent",
        "description": (
            f"Remote clive instance at {actual_host}. "
            f"Peer conversation via TURN: protocol."
        ),
        "host": actual_host,
        "connect_timeout": config.get("timeout", 5),
        "category": "agent",
    }


def build_agent_ssh_cmd(host: str, config: dict) -> str:
    """Build SSH command for clive-to-clive connection.

    No -t flag (no TTY) → inner clive auto-detects conversational mode.
    Forwards API key env vars via SendEnv (BYOLLM).
    """
    parts = ["ssh"]

    # SSH key
    key = config.get("key")
    if key:
        parts.append(f"-i {key}")

    # Forward API key env vars
    for env_var in _FORWARD_ENVS:
        if os.environ.get(env_var):
            parts.append(f"-o SendEnv={env_var}")

    # Connection options
    parts.extend(["-o BatchMode=yes", "-o ConnectTimeout=10"])

    # Host
    parts.append(host)

    # Remote command
    clive_path = config.get("path", DEFAULT_CLIVE_PATH)
    toolset = config.get("toolset")
    remote_parts = [clive_path, "--conversational"]
    if toolset:
        remote_parts.extend(["-t", toolset])

    remote_cmd = " ".join(remote_parts)
    parts.append(f"'{remote_cmd}'")

    return " ".join(parts)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_agents.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add agents.py tests/test_agents.py
git commit -m "feat: add agents.py — clive@host address parsing and resolution"
```

---

### Task 2: Turn-state protocol parsing (`remote.py`)

**Files:**
- Modify: `remote.py:27-37`
- Modify: `tests/test_remote.py`

**Step 1: Write the failing tests**

Add to `tests/test_remote.py`:

```python
from remote import parse_turn_state, parse_context


# ─── Turn state parsing ──────────────────────────────────────────────────────

def test_parse_turn_thinking():
    screen = "PROGRESS: step 1\nTURN: thinking"
    assert parse_turn_state(screen) == "thinking"


def test_parse_turn_waiting():
    screen = 'QUESTION: "which one?"\nTURN: waiting'
    assert parse_turn_state(screen) == "waiting"


def test_parse_turn_done():
    screen = 'CONTEXT: {"result": "found it"}\nTURN: done'
    assert parse_turn_state(screen) == "done"


def test_parse_turn_failed():
    screen = 'CONTEXT: {"error": "timeout"}\nTURN: failed'
    assert parse_turn_state(screen) == "failed"


def test_parse_turn_none():
    """No TURN: line → None (still working or not conversational)."""
    screen = "some output\nstill running..."
    assert parse_turn_state(screen) is None


def test_parse_turn_latest_wins():
    """Multiple TURN: lines → last one wins."""
    screen = "TURN: thinking\nPROGRESS: step 2\nTURN: waiting"
    assert parse_turn_state(screen) == "waiting"


# ─── Context parsing ─────────────────────────────────────────────────────────

def test_parse_context_json():
    screen = 'CONTEXT: {"result": "hello", "files": ["a.txt"]}\nTURN: done'
    ctx = parse_context(screen)
    assert ctx["result"] == "hello"
    assert ctx["files"] == ["a.txt"]


def test_parse_context_last_wins():
    screen = 'CONTEXT: {"step": 1}\nCONTEXT: {"step": 2, "result": "final"}\nTURN: done'
    ctx = parse_context(screen)
    assert ctx["step"] == 2


def test_parse_context_none():
    screen = "no context here\nTURN: done"
    assert parse_context(screen) is None
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_remote.py::test_parse_turn_thinking -v`
Expected: FAIL with `ImportError: cannot import name 'parse_turn_state'`

**Step 3: Add `parse_turn_state` and `parse_context` to `remote.py`**

Add after line 57 (after `parse_remote_files`):

```python
def parse_turn_state(screen: str) -> str | None:
    """Parse the latest TURN: state from screen content.

    Returns "thinking", "waiting", "done", "failed", or None.
    When multiple TURN: lines exist, the last one wins.
    """
    state = None
    for line in screen.splitlines():
        stripped = line.strip()
        if stripped.startswith("TURN:"):
            state = stripped[5:].strip().lower()
    return state


def parse_context(screen: str) -> dict | None:
    """Parse the latest CONTEXT: JSON from screen content.

    When multiple CONTEXT: lines exist, the last one wins.
    Returns parsed dict or None.
    """
    ctx = None
    for line in screen.splitlines():
        stripped = line.strip()
        if stripped.startswith("CONTEXT:"):
            payload = stripped[8:].strip()
            try:
                ctx = json.loads(payload)
            except json.JSONDecodeError:
                ctx = {"raw": payload}
    return ctx
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_remote.py -v`
Expected: all PASS (old + new)

**Step 5: Commit**

```bash
git add remote.py tests/test_remote.py
git commit -m "feat: add TURN:/CONTEXT: protocol parsing to remote.py"
```

---

### Task 3: Conversational output skin (`output.py`)

**Files:**
- Modify: `output.py:13-14` (add `_conversational` flag)
- Modify: `output.py` (wrap public functions)
- Create: `tests/test_output_conversational.py`

**Step 1: Write the failing tests**

```python
# tests/test_output_conversational.py
"""Tests for conversational output mode."""
import io
import sys
from output import (
    set_conversational, is_conversational,
    emit_turn, emit_context, emit_question,
)


def test_set_conversational():
    set_conversational(True)
    assert is_conversational()
    set_conversational(False)
    assert not is_conversational()


def test_emit_turn(capsys):
    set_conversational(True)
    try:
        emit_turn("thinking")
        captured = capsys.readouterr()
        assert "TURN: thinking" in captured.out
    finally:
        set_conversational(False)


def test_emit_context(capsys):
    set_conversational(True)
    try:
        emit_context({"result": "hello"})
        captured = capsys.readouterr()
        assert "CONTEXT:" in captured.out
        assert '"result"' in captured.out
    finally:
        set_conversational(False)


def test_emit_question(capsys):
    set_conversational(True)
    try:
        emit_question("Which one do you want?")
        captured = capsys.readouterr()
        assert 'QUESTION: "Which one do you want?"' in captured.out
    finally:
        set_conversational(False)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_output_conversational.py -v`
Expected: FAIL with `ImportError`

**Step 3: Add conversational mode to `output.py`**

After line 14 (`_active = None`), add:

```python
_conversational = False
```

After `is_quiet()` (line 100), add:

```python
def set_conversational(enabled: bool):
    """Enable/disable conversational output mode (clive-to-clive)."""
    global _conversational
    _conversational = enabled


def is_conversational() -> bool:
    """Check if conversational mode is active."""
    return _conversational
```

After `result()` (line 159), add:

```python
# --- Conversational protocol ---

def emit_turn(state: str):
    """Emit TURN: protocol line. States: thinking, waiting, done, failed."""
    print(f"TURN: {state}", flush=True)


def emit_context(data: dict):
    """Emit CONTEXT: protocol line with JSON payload."""
    import json
    print(f"CONTEXT: {json.dumps(data)}", flush=True)


def emit_question(question: str):
    """Emit QUESTION: protocol line."""
    print(f'QUESTION: "{question}"', flush=True)
```

Modify `progress()` (line 103) to route through conversational skin:

```python
def progress(msg: str):
    """Legacy progress output. Stops any active animation first."""
    if _conversational:
        print(f"PROGRESS: {msg}", flush=True)
        return
    with _lock:
        _stop_active()
    print(msg, file=_stream())
```

Modify `step()` (line 110) similarly:

```python
def step(msg: str):
    """Major step marker with pulsating ⏺."""
    if _conversational:
        print(f"PROGRESS: {msg}", flush=True)
        return
    global _active
    with _lock:
        _stop_active()
        s = _stream()
        if _is_tty():
            s.write("\n")
            s.flush()
            _active = _Pulse("⏺", msg, s)
        else:
            print(f"\n⏺ {msg}", file=s)
```

Modify `detail()` (line 124) similarly:

```python
def detail(msg: str):
    """Indented detail line. Replaces any active activity pulse."""
    if _conversational:
        print(f"PROGRESS: {msg}", flush=True)
        return
    global _active
    with _lock:
        if _active and _active.indent:
            _active.replace(f"  {msg}")
            _active = None
        else:
            _stop_active()
            print(f"  {msg}", file=_stream())
```

Modify `activity()` (line 137) similarly:

```python
def activity(msg: str):
    """In-progress activity line with pulsating ◌ indicator."""
    if _conversational:
        print(f"PROGRESS: {msg}", flush=True)
        return
    global _active
    with _lock:
        _stop_active()
        s = _stream()
        if _is_tty():
            _active = _Pulse("◌", msg, s, indent="  ")
        else:
            print(f"  ◌ {msg}", file=s)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_output_conversational.py -v`
Expected: all PASS

**Step 5: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: all PASS

**Step 6: Commit**

```bash
git add output.py tests/test_output_conversational.py
git commit -m "feat: add conversational output skin to output.py"
```

---

### Task 4: Lazy pane injection (`session.py`)

**Files:**
- Modify: `session.py` (add `ensure_agent_pane`)
- Create: `tests/test_agent_pane.py`

**Step 1: Write the failing test**

```python
# tests/test_agent_pane.py
"""Tests for dynamic agent pane injection."""
from session import ensure_agent_pane


def test_ensure_agent_pane_returns_pane_def():
    """Verify ensure_agent_pane returns the right structure (unit test only, no tmux)."""
    # This is a structural test — we mock the tmux interaction
    from unittest.mock import MagicMock, patch
    from models import PaneInfo

    mock_session = MagicMock()
    mock_window = MagicMock()
    mock_pane_obj = MagicMock()
    mock_session.new_window.return_value = mock_window
    mock_window.active_pane = mock_pane_obj
    mock_pane_obj.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])

    panes = {"shell": MagicMock(spec=PaneInfo)}
    config = {
        "cmd": "ssh localhost 'python3 clive.py --conversational'",
        "host": "localhost",
        "connect_timeout": 1,
        "app_type": "agent",
    }

    with patch("time.sleep"):
        result = ensure_agent_pane(mock_session, panes, "localhost", config)

    assert "agent-localhost" in panes
    assert isinstance(result, PaneInfo)
    assert result.app_type == "agent"
    assert result.name == "agent-localhost"


def test_ensure_agent_pane_reuses_existing():
    """If pane already exists, return it without creating a new one."""
    from unittest.mock import MagicMock
    from models import PaneInfo

    mock_session = MagicMock()
    existing_pane = MagicMock(spec=PaneInfo)
    existing_pane.app_type = "agent"
    existing_pane.name = "agent-localhost"
    panes = {"agent-localhost": existing_pane}

    result = ensure_agent_pane(mock_session, panes, "localhost", {})
    assert result is existing_pane
    mock_session.new_window.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agent_pane.py -v`
Expected: FAIL with `ImportError: cannot import name 'ensure_agent_pane'`

**Step 3: Implement `ensure_agent_pane` in `session.py`**

Add after `capture_pane()` (after line 125):

```python
def ensure_agent_pane(
    session: libtmux.Session,
    panes: dict[str, PaneInfo],
    host: str,
    config: dict,
) -> PaneInfo:
    """Lazily create an agent pane for clive@host if it doesn't exist.

    If agent-{host} already exists in panes, returns it.
    Otherwise creates a new tmux window, opens SSH, and adds to panes.
    """
    pane_name = f"agent-{host}"

    if pane_name in panes:
        return panes[pane_name]

    window = session.new_window(window_name=pane_name, attach=False)
    pane = window.active_pane

    cmd = config.get("cmd", f"ssh {host}")
    pane.send_keys(cmd, enter=True)
    time.sleep(config.get("connect_timeout", 3))

    pane_info = PaneInfo(
        pane=pane,
        app_type=config.get("app_type", "agent"),
        description=config.get("description", f"Remote clive at {host}"),
        name=pane_name,
        idle_timeout=config.get("idle_timeout", 5.0),
    )
    panes[pane_name] = pane_info

    progress(f"  ✓ {pane_name} [agent] connected")
    return pane_info
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_agent_pane.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add session.py tests/test_agent_pane.py
git commit -m "feat: add ensure_agent_pane for lazy pane injection"
```

---

### Task 5: Executor turn-state handling (`executor.py`)

**Files:**
- Modify: `executor.py:1037-1058`

**Step 1: Read the current code**

Reference: `executor.py:1037-1058` (already read above)

**Step 2: Replace the agent pane block**

Replace lines 1037-1058 in `executor.py`:

Old code:
```python
            # Check for DONE: protocol (clive-to-clive, agent panes only)
            if pane_info.app_type == "agent":
                from remote import parse_remote_result, parse_remote_files, parse_remote_progress
                done = parse_remote_result(screen)
                if done:
                    summary = done.get("result", done.get("reason", str(done)))
                    status = SubtaskStatus.COMPLETED if done.get("status") == "success" else SubtaskStatus.FAILED

                    # Log progress lines if any
                    for prog in parse_remote_progress(screen):
                        logging.debug(f"[{subtask.id}] Remote: {prog}")

                    # Track declared files for downstream transfer
                    remote_files = done.get("files", []) + parse_remote_files(screen)
                    output_files = [{"path": f, "type": "remote", "size": 0} for f in remote_files]

                    return SubtaskResult(
                        subtask_id=subtask.id, status=status, summary=summary,
                        output_snippet=screen[-500:], turns_used=turn,
                        prompt_tokens=total_pt, completion_tokens=total_ct,
                        output_files=output_files,
                    )
```

New code:
```python
            # Check for TURN:/DONE: protocol (clive-to-clive, agent panes only)
            if pane_info.app_type == "agent":
                from remote import (
                    parse_turn_state, parse_context,
                    parse_remote_result, parse_remote_files, parse_remote_progress,
                )
                turn_state = parse_turn_state(screen)

                # New conversational protocol (TURN:)
                if turn_state in ("done", "failed"):
                    ctx = parse_context(screen) or {}
                    summary = ctx.get("result", ctx.get("error", ctx.get("raw", str(ctx))))
                    status = SubtaskStatus.COMPLETED if turn_state == "done" else SubtaskStatus.FAILED

                    for prog in parse_remote_progress(screen):
                        logging.debug(f"[{subtask.id}] Remote: {prog}")

                    remote_files = ctx.get("files", []) + parse_remote_files(screen)
                    output_files = [{"path": f, "type": "remote", "size": 0} for f in remote_files]

                    return SubtaskResult(
                        subtask_id=subtask.id, status=status, summary=summary,
                        output_snippet=screen[-500:], turns_used=turn,
                        prompt_tokens=total_pt, completion_tokens=total_ct,
                        output_files=output_files,
                    )

                if turn_state == "thinking":
                    # Inner clive is working — skip LLM call, save tokens
                    logging.debug(f"[{subtask.id}] Agent thinking, skipping LLM call")
                    last_screen = screen
                    time.sleep(2)
                    continue

                # turn_state == "waiting" or None → fall through to LLM loop
                # "waiting" = inner clive wants input, LLM will read and respond
                # None = legacy DONE: protocol or still starting up

                # Backward compat: check legacy DONE: protocol
                if turn_state is None:
                    done = parse_remote_result(screen)
                    if done:
                        summary = done.get("result", done.get("reason", str(done)))
                        status = SubtaskStatus.COMPLETED if done.get("status") == "success" else SubtaskStatus.FAILED

                        for prog in parse_remote_progress(screen):
                            logging.debug(f"[{subtask.id}] Remote: {prog}")

                        remote_files = done.get("files", []) + parse_remote_files(screen)
                        output_files = [{"path": f, "type": "remote", "size": 0} for f in remote_files]

                        return SubtaskResult(
                            subtask_id=subtask.id, status=status, summary=summary,
                            output_snippet=screen[-500:], turns_used=turn,
                            prompt_tokens=total_pt, completion_tokens=total_ct,
                            output_files=output_files,
                        )
```

**Step 3: Run existing tests**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: all PASS

**Step 4: Commit**

```bash
git add executor.py
git commit -m "feat: turn-state-aware agent pane handling in executor"
```

---

### Task 6: Agent driver prompt (`drivers/agent.md`)

**Files:**
- Modify: `drivers/agent.md`

**Step 1: Rewrite the driver prompt**

Replace entire contents of `drivers/agent.md`:

```markdown
# Agent Driver (clive-to-clive peer conversation)

ENVIRONMENT: connected to a remote clive instance via SSH.
The remote clive runs in conversational mode (structured turn protocol).

PROTOCOL (read from pane screen):
  TURN: thinking    — remote is working. DO NOT type. Wait.
  TURN: waiting     — remote needs input. Read QUESTION/CONTEXT, respond.
  TURN: done        — task complete. Extract result from last CONTEXT line.
  TURN: failed      — task failed. Extract error from last CONTEXT line.

  CONTEXT: {...}    — structured JSON state from remote
  QUESTION: "..."   — question from remote (read before responding)
  PROGRESS: ...     — status update (informational only)
  FILE: filename    — file available for scp transfer

RULES:
- ONLY type when TURN: waiting appears. Never interrupt TURN: thinking.
- Read QUESTION and CONTEXT lines before composing your response.
- Keep responses concise and actionable — the remote clive parses your text.
- You are a peer, not a supervisor. The remote clive has its own judgment.
- If TURN: done result is insufficient, send a follow-up task on a new line.

SENDING THE INITIAL TASK:
  Type the task description as a single line, press Enter.
  <cmd type="wait">10</cmd>

RESPONDING TO QUESTIONS:
  Read the QUESTION line. Type your answer as a single line, press Enter.
  <cmd type="wait">10</cmd>

LEGACY PROTOCOL (backward compatibility):
  DONE: {"status": "success", "result": "..."}  — older clive instances
  DONE: {"status": "error", "reason": "..."}     — older error format

COMPLETION:
  Use <cmd type="task_complete">summary from CONTEXT</cmd> after TURN: done.
  Include key results. If FILE: lines appeared, note files for transfer.
```

**Step 2: Verify driver loads**

Run: `python3 -c "from prompts import load_driver; d = load_driver('agent'); print('TURN:' in d, len(d))"`
Expected: `True <length>`

**Step 3: Commit**

```bash
git add drivers/agent.md
git commit -m "feat: rewrite agent driver for peer conversation protocol"
```

---

### Task 7: Planner agent-address awareness (`planner.py`)

**Files:**
- Modify: `planner.py:12-72`
- Modify: `clive.py:312-382` (three-tier routing)

**Step 1: Modify `_run_inner` in `clive.py` to detect `clive@` addresses**

In `clive.py`, before the three-tier intent resolution block (line 312), add agent address extraction:

After line 309 (`start_time = time.time()`), add:

```python
    # ─── Agent Address Resolution ──────────────────────────────────────
    from agents import parse_agent_addresses
    agent_addresses = parse_agent_addresses(task)

    if agent_addresses:
        # Extract the first agent address (multi-agent handled by planner)
        agent_host, inner_task = agent_addresses[0]
        from agents import resolve_agent
        from session import ensure_agent_pane

        agent_config = resolve_agent(agent_host)
        # Get session from the first pane's session
        first_pane = list(panes.values())[0]
        tmux_session = first_pane.pane.window.session

        ensure_agent_pane(tmux_session, panes, agent_host, agent_config)

        # Route directly to agent pane — skip classifier/planner
        pane_name = f"agent-{agent_host}"
        plan = Plan(task=task, subtasks=[
            Subtask(
                id="1",
                description=inner_task,
                pane=pane_name,
                mode="interactive",
            ),
        ])
        step("Routing")
        detail(f"Agent: clive@{agent_host}")
        display_plan(plan)

        # Update tool_status for the new pane
        tool_status[pane_name] = {
            "status": "ready",
            "app_type": "agent",
            "description": agent_config["description"],
        }
```

Then modify the existing three-tier block to check `if plan is None:` before Tier 0 (it already does this for Tier 1 and 2):

Change line 317 from:
```python
    if _is_direct(task, len(panes)):
```
to:
```python
    if plan is None and _is_direct(task, len(panes)):
```

**Step 2: Run existing tests**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: all PASS

**Step 3: Commit**

```bash
git add clive.py
git commit -m "feat: clive@ address extraction and routing in main flow"
```

---

### Task 8: Conversational mode for inner Clive (`clive.py`)

**Files:**
- Modify: `clive.py` (add `--conversational` flag + mode)

**Step 1: Add the `--conversational` argument**

After line 641 (`"--json"` argument), add:

```python
    parser.add_argument(
        "--conversational",
        action="store_true",
        help="Conversational mode for clive-to-clive peer dialogue (auto-detected via isatty)",
    )
```

**Step 2: Add auto-detection and conversational main loop**

After the `--remote` handler (after line 952), add:

```python
    # ─── Mode auto-detection ──────────────────────────────────────────
    # Conversational mode: explicit flag or no TTY (clive-to-clive via SSH)
    if args.conversational or (
        not sys.stdin.isatty()
        and not args.quiet
        and not args.json
        and not args.oneline
        and not args.bool
        and args.task
    ):
        from output import set_conversational, emit_turn, emit_context, emit_question
        set_conversational(True)

        # Read task from args (SSH command) or stdin
        task = args.task
        if not task:
            try:
                task = sys.stdin.readline().strip()
            except EOFError:
                emit_turn("failed")
                raise SystemExit(1)

        if not task:
            emit_context({"error": "No task provided"})
            emit_turn("failed")
            raise SystemExit(1)

        emit_turn("thinking")

        try:
            summary = run(
                task,
                toolset_spec=args.toolset,
                output_format="default",
                max_tokens=args.max_tokens,
            )
            emit_context({"result": summary})
            emit_turn("done")
        except Exception as e:
            emit_context({"error": str(e)})
            emit_turn("failed")
            raise SystemExit(1)

        raise SystemExit(0)
```

**Step 3: Verify the flag registers**

Run: `python3 clive.py --help | grep conversational`
Expected: shows the `--conversational` help text

**Step 4: Run existing tests**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: all PASS

**Step 5: Commit**

```bash
git add clive.py
git commit -m "feat: --conversational flag with isatty auto-detection"
```

---

### Task 9: Integration test — loopback conversation

**Files:**
- Modify: `tests/test_loopback.sh`

**Step 1: Update the loopback test script**

Replace contents of `tests/test_loopback.sh`:

```bash
#!/usr/bin/env bash
# Test two Clive instances talking via clive@localhost addressing.
# Usage: bash tests/test_loopback.sh
#
# Observe:
#   Terminal 1: this script (shows outer Clive output)
#   Terminal 2: tmux attach -t clive   (watch panes live)
#   Logs:       tail -f /tmp/clive/*/clive.log

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Clive Loopback Test (clive@localhost addressing) ==="
echo ""
echo "To observe live, open another terminal and run:"
echo "  tmux attach -t clive"
echo ""
echo "Starting outer Clive..."
echo ""

python3 clive.py \
    --debug \
    --max-tokens 30000 \
    "clive@localhost read https://news.ycombinator.com and give me a summary on anthropic mythos"
```

**Step 2: Verify syntax**

Run: `bash -n tests/test_loopback.sh`
Expected: no output (syntax OK)

**Step 3: Commit**

```bash
git add tests/test_loopback.sh
git commit -m "feat: update loopback test for clive@localhost addressing"
```

---

### Task 10: Registry setup and cleanup

**Files:**
- Create: `~/.clive/agents.yaml` (user config, not committed)
- Modify: `toolsets.py` (remove loopback entries)

**Step 1: Create the registry file**

```bash
mkdir -p ~/.clive
cat > ~/.clive/agents.yaml << 'EOF'
# Clive agent registry — clive@host resolution
# Entries override auto-resolve defaults.
# All fields optional. Host defaults to the entry name.

localhost:
  toolset: web
EOF
```

**Step 2: Remove loopback entries from toolsets.py**

Remove the `localhost_agent` pane, `loopback` category, and `loopback` profile from `toolsets.py` — `clive@localhost` replaces them.

**Step 3: Run all tests**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: all PASS

**Step 4: Commit**

```bash
git add toolsets.py
git commit -m "refactor: remove loopback entries — replaced by clive@host addressing"
```

---

### Task 11: Full end-to-end test

**Step 1: Run the loopback test**

Run: `bash tests/test_loopback.sh`

**Step 2: Observe in tmux**

In another terminal: `tmux attach -t clive`

Verify:
- `agent-localhost` pane appears
- SSH connection established
- Inner Clive emits `TURN: thinking`, `PROGRESS:` lines
- Outer Clive waits during `thinking`, responds during `waiting`
- Final `TURN: done` terminates the subtask
- Outer Clive summarizes and exits

**Step 3: Check logs**

Run: `tail -f /tmp/clive/*/clive.log`

Verify debug lines show:
- `Agent thinking, skipping LLM call` during thinking turns
- Protocol parsing of TURN/CONTEXT/QUESTION lines
