# Instance Dashboard & Local Addressing — Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give every clive instance a name, make them discoverable via a file-based registry, addressable via the existing `clive@name` scheme (local-first resolution), and visible through a dashboard (snapshot CLI + live TUI view).

**Architecture:** File-based instance registry at `~/.clive/instances/`, local address resolution as a new first step in the existing `clive@host` chain, conversational tmux pane per named instance, dashboard as both a CLI flag and a TUI slash command.

**Tech Stack:** Python 3.12, libtmux, existing agents.py/remote.py, existing tui.py (textual), `os.kill(pid, 0)` for liveness checks.

---

## Core Invariant

**If you have a name, you're conversational.** A named instance stays alive after its initial task, listening for more work on a dedicated `conversational` tmux window. This is the contract that makes local addressing work.

---

## 1. Instance Identity & Registry

### Naming

- Explicit: `clive --name mybot "task"` or `clive --name mybot --tui`
- Auto-fallback for unnamed instances: `{hostname}-{pid}` (but unnamed instances are not conversational and not addressable)
- Name collisions rejected at startup: if `~/.clive/instances/mybot.json` exists and PID is alive, fail with `"Instance 'mybot' is already running (PID 48201)"`

### Registry

Directory: `~/.clive/instances/`, one JSON file per instance.

```json
{
    "name": "mybot",
    "pid": 48201,
    "tmux_session": "clive-a1b2c3d4",
    "tmux_socket": "clive",
    "toolset": "standard+media",
    "task": "monitoring server logs",
    "conversational": true,
    "started_at": 1744195200.0,
    "session_dir": "/tmp/clive/a1b2c3d4"
}
```

- Written on startup, deleted on exit (cleanup handler)
- Stale detection: `os.kill(pid, 0)` — if PID is dead, prune the file
- No daemon, no socket, no coordination needed

---

## 2. Local Address Resolution

The existing `agents.py` resolution chain gets a new first step. When `clive@mybot` is encountered:

1. **Check local registry** — `~/.clive/instances/mybot.json`. If found, PID alive, `conversational: true` → local resolution
2. **Check agents.yaml** — existing remote registry (unchanged)
3. **Auto-resolve via SSH** — existing fallback (unchanged)

Local resolution returns a pane definition using tmux attach instead of SSH:

```python
{
    "name": "agent-mybot",
    "cmd": "tmux -L clive attach -t clive-a1b2c3d4:conversational",
    "app_type": "agent",
    "description": "Local clive instance 'mybot'",
    "host": None,  # None = local, no SCP needed
}
```

The outer clive opens a pane in its own session, runs the tmux attach command, and has a live view of mybot's conversational pane. From the executor's perspective, this pane is identical to a remote agent pane — same TURN: protocol, same parsing, same timeout handling. Only difference: microsecond latency instead of milliseconds.

If target isn't conversational → resolution falls through to SSH.

A local instance can "shadow" a remote host with the same name — local registry is checked first. This is useful for testing.

---

## 3. The Conversational Pane

Named instances create an extra tmux window called `conversational`:

```
tmux session "clive-a1b2c3d4"
  ├── window: shell           (normal work pane)
  ├── window: browser         (normal work pane)
  └── window: conversational  (listening for tasks)
```

The conversational pane runs clive in a stdin-reading loop. When a task arrives (typed by an outer clive or a human attaching):

```
TURN: thinking
PROGRESS: step 1 of 2 — downloading report
PROGRESS: step 2 of 2 — extracting tables
CONTEXT: {"files": ["report.csv"], "summary": "Q4 revenue: $4.2M"}
TURN: done
DONE: {"status": "success", "result": "Extracted revenue figures"}
```

Between tasks, the pane shows an idle prompt signaling readiness.

Key advantage over subprocess/queue: the second task inherits the environment, working directory, and pane agent memory from the first. The instance accumulates knowledge across tasks.

---

## 4. Instance Lifecycle

```bash
# Unnamed: run and exit (current behavior, unchanged)
clive "count files in /tmp"

# Named: run initial task, then keep listening
clive --name mybot "count files in /tmp"

# Named, no initial task: just listen
clive --name mybot

# TUI: always stays alive, name makes it addressable
clive --name mybot --tui
```

Lifecycle:
1. Start with `--name` → register in `~/.clive/instances/` → create conversational pane → run initial task if given → wait for more
2. Receive task on conversational pane → plan → execute → emit TURN: done → wait for more
3. Receive SIGTERM (or `/stop`) → clean up tmux session → delete registry file → exit

Stopping: `clive --stop mybot` sends SIGTERM to the PID from the registry. The instance cleans up and deregisters.

---

## 5. The Dashboard

### Snapshot CLI: `clive --dashboard`

```
$ clive --dashboard

 CLIVE INSTANCES
 ───────────────────────────────────────────────────────
  NAME          PID     TOOLSET          STATUS    UPTIME
  mybot         48201   standard+media   idle      2h 14m
  researcher    48305   research+web     working   0h 03m
  gpu-worker    48410   standard+ai      idle      5h 41m

 TASKS IN PROGRESS
 ───────────────────────────────────────────────────────
  researcher    "analyze competitor pricing from 5 sites"
                step 2/4 · browser pane · 1,240 tokens

 3 instances · 1 busy · 0 queued
```

- Reads `~/.clive/instances/*.json`, prunes dead PIDs
- Peeks at each instance's tmux session to detect TURN: state (idle vs working)
- Prints and exits — like `docker ps`
- Also shows remote instances from `~/.clive/agents.yaml` with `remote` tag and connectivity indicator

### TUI live view: `/dashboard`

- Slash command in TUI toggles a live-updating panel
- Same data as snapshot, refreshed every 2 seconds
- Can type `clive@mybot do something` directly from the dashboard input line — routes through normal address resolution
- Future: `/stop mybot` command to terminate instances from TUI

---

## Implementation Tasks

### Task 1: Instance registry module

**Files:**
- Create: `registry.py`
- Test: `tests/test_registry.py`

```python
# registry.py — core functions
def register(name: str, pid: int, tmux_session: str, tmux_socket: str,
             toolset: str, task: str, conversational: bool, session_dir: str) -> Path

def deregister(name: str) -> bool

def list_instances() -> list[dict]  # prunes dead PIDs automatically

def get_instance(name: str) -> dict | None  # returns None if dead/missing

def is_name_available(name: str) -> bool
```

**Tests:**
```python
def test_register_creates_file(tmp_path):
    register("mybot", pid=os.getpid(), ..., registry_dir=tmp_path)
    assert (tmp_path / "mybot.json").exists()

def test_deregister_removes_file(tmp_path): ...

def test_list_prunes_dead_pids(tmp_path):
    # Write a registry entry with a dead PID
    # list_instances() should not return it and should delete the file

def test_name_collision_detected(tmp_path):
    register("mybot", pid=os.getpid(), ..., registry_dir=tmp_path)
    assert not is_name_available("mybot", registry_dir=tmp_path)

def test_name_available_after_pid_dies(tmp_path):
    # Write entry with PID 99999999 (not running)
    assert is_name_available("mybot", registry_dir=tmp_path)
```

**Step 1:** Write failing tests
**Step 2:** Run tests, verify fail
**Step 3:** Implement `registry.py`
**Step 4:** Run tests, verify pass
**Step 5:** Commit: `feat(registry): add file-based instance registry`

---

### Task 2: --name flag and instance lifecycle

**Files:**
- Modify: `clive.py` (add `--name`, `--stop` args, register/deregister in lifecycle)
- Modify: `session.py` (create conversational pane for named instances)
- Test: `tests/test_instance_lifecycle.py`

**Tests:**
```python
def test_name_flag_registers_instance(tmp_path):
    # Mock the registry dir, simulate startup with --name
    # Verify registry file created

def test_exit_deregisters_instance(tmp_path):
    # Simulate cleanup handler
    # Verify registry file removed

def test_stop_sends_sigterm(tmp_path):
    # Register a fake instance with current PID
    # Call stop logic, verify signal sent

def test_conversational_pane_created_for_named():
    # Verify session has a "conversational" window when --name is used
```

**Step 1:** Write failing tests
**Step 2:** Add `--name` and `--stop` to argparse in `clive.py`
**Step 3:** Wire register/deregister into `run()` lifecycle and cleanup handler
**Step 4:** Add conversational pane creation in `session.py`
**Step 5:** Run tests, verify pass
**Step 6:** Commit: `feat(instance): --name flag with register/deregister lifecycle`

---

### Task 3: Local address resolution

**Files:**
- Modify: `agents.py` (add local registry check as first resolution step)
- Test: `tests/test_local_resolution.py`

**Tests:**
```python
def test_local_resolution_finds_registry_entry(tmp_path):
    # Write a registry entry for "mybot" with current PID
    # resolve_agent("mybot") should return local pane definition with tmux attach cmd

def test_local_resolution_falls_through_when_not_found(tmp_path):
    # No registry entry for "mybot"
    # resolve_agent("mybot") should return SSH-based definition (existing behavior)

def test_local_resolution_skips_non_conversational(tmp_path):
    # Registry entry exists but conversational=false
    # Should fall through to SSH

def test_local_pane_has_no_host():
    # Local resolution must set host=None (signals no SCP needed)

def test_local_shadows_remote(tmp_path):
    # Both local registry and agents.yaml have "mybot"
    # Local wins
```

**Step 1:** Write failing tests
**Step 2:** Add `_check_local_registry()` to `agents.py`, call it first in `resolve_agent()`
**Step 3:** Run tests, verify pass
**Step 4:** Commit: `feat(agents): local-first address resolution via instance registry`

---

### Task 4: Dashboard snapshot CLI

**Files:**
- Create: `dashboard.py`
- Modify: `clive.py` (add `--dashboard` flag)
- Test: `tests/test_dashboard.py`

**Tests:**
```python
def test_dashboard_lists_instances(tmp_path, capsys):
    # Register two instances
    # Call dashboard render
    # Verify both appear in output

def test_dashboard_prunes_dead(tmp_path, capsys):
    # Register instance with dead PID
    # Dashboard should not show it

def test_dashboard_shows_remote_from_agents_yaml(tmp_path): ...

def test_dashboard_empty_state(tmp_path, capsys):
    # No instances
    # Shows "No instances running."
```

**Step 1:** Write failing tests
**Step 2:** Implement `dashboard.py` with `render_snapshot()` function
**Step 3:** Wire `--dashboard` flag in `clive.py` to call `render_snapshot()` and exit
**Step 4:** Run tests, verify pass
**Step 5:** Commit: `feat(dashboard): add --dashboard snapshot command`

---

### Task 5: TUI /dashboard slash command

**Files:**
- Modify: `tui.py` (add `/dashboard` handler, live-updating panel)
- Test: `tests/test_tui_dashboard.py`

**Tests:**
```python
def test_slash_dashboard_recognized():
    # Verify /dashboard is in the command handler

def test_dashboard_output_contains_instance_table():
    # Mock registry, trigger /dashboard, verify RichLog output
```

**Step 1:** Write failing tests
**Step 2:** Add `/dashboard` to the slash command handler in `tui.py`
**Step 3:** Render instance table using `dashboard.render_snapshot()` into the RichLog
**Step 4:** Add 2-second refresh timer when dashboard is active
**Step 5:** Update HELP_TEXT with `/dashboard`
**Step 6:** Run tests, verify pass
**Step 7:** Commit: `feat(tui): add /dashboard slash command with live refresh`

---

### Task 6: Conversational loop completion

**Files:**
- Modify: `clive.py` (complete the `--conversational` stdin loop for named instances)
- Test: `tests/test_conversational_named.py`

**Tests:**
```python
def test_named_instance_stays_alive_after_task():
    # Start clive --name mybot with a task
    # Verify process doesn't exit after task completes

def test_conversational_pane_emits_turn_protocol():
    # Send a task to the conversational pane
    # Verify TURN: thinking, TURN: done, DONE: appear in output

def test_conversational_accepts_second_task():
    # Send task 1, wait for DONE:
    # Send task 2, wait for DONE:
    # Both should complete
```

**Step 1:** Write failing tests
**Step 2:** Implement the stdin-reading loop in `clive.py` for named conversational instances
**Step 3:** Emit TURN:/PROGRESS:/CONTEXT:/DONE: markers during execution
**Step 4:** After task completion, loop back to reading stdin
**Step 5:** Run tests, verify pass
**Step 6:** Commit: `feat(conversational): complete stdin loop for named instances`

---

## Implementation Order

```
Task 1 (registry) → Task 2 (--name lifecycle) → Task 3 (local resolution)
                                                        ↓
Task 4 (dashboard CLI) ─────────────────────→ Task 5 (TUI /dashboard)
        ↓
Task 6 (conversational loop)
```

Tasks 1→2→3 are sequential (each builds on the previous).
Task 4 can start after Task 1 (only needs registry).
Task 5 needs Task 4.
Task 6 needs Task 2.

---

## Open Questions (deferred)

- **Dashboard: tmux pane peek for status** — reading TURN: state from another instance's tmux session requires cross-session capture. Doable (`tmux -L clive capture-pane -t session:window`) but adds complexity. Defer to v2.
- **Instance groups** — `clive --name worker --group gpu-pool` for load-balanced addressing. Future work.
- **Auto-naming in TUI** — should TUI mode auto-assign a name? Probably yes, but let users opt in first.
