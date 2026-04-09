# Loopback Agent Test — Two Clive Instances Talking

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable two Clive instances to communicate via SSH localhost loopback, with full observability via tmux + logs.

**Architecture:** Add a `localhost_agent` pane to toolsets.py that SSHs to localhost and runs `clive --quiet --json -t web`. The outer Clive orchestrates via the agent protocol (DONE:/PROGRESS:). Both instances log to files. Observer watches via `tmux attach`.

**Tech Stack:** SSH loopback, tmux, existing agent protocol (remote.py)

---

### Task 1: Add localhost_agent pane and loopback profile to toolsets.py

**Files:**
- Modify: `toolsets.py:134-147` (add new pane after remote_agent)
- Modify: `toolsets.py:431` (add loopback category)
- Modify: `toolsets.py:449` (add loopback profile)

**Step 1: Add localhost_agent pane definition**

In `toolsets.py`, after the `remote_agent` entry (line 146), add:

```python
    "localhost_agent": {
        "name": "agent",
        "cmd": "ssh localhost 'cd ~/Documents/Development/clive && python3 clive.py --quiet --json -t web'",
        "app_type": "agent",
        "description": (
            "Local clive instance via SSH loopback. Send tasks as plain text, read results. "
            "Uses DONE: JSON protocol for structured responses."
        ),
        "host": "localhost",
        "connect_timeout": 3,
        "category": "loopback",
    },
```

**Step 2: Add loopback category**

In `CATEGORIES` dict (around line 431), add:

```python
    "loopback":     {"panes": ["localhost_agent"],       "commands": [],                                         "endpoints": []},
```

**Step 3: Add loopback profile**

In `PROFILES` dict (around line 449), add:

```python
    "loopback": ["core", "loopback"],
```

**Step 4: Verify syntax**

Run: `python3 -c "from toolsets import resolve_toolset; r = resolve_toolset('loopback'); print([p['name'] for p in r['panes']])"`
Expected: `['shell', 'agent']`

---

### Task 2: Create test runner script

**Files:**
- Create: `tests/test_loopback.sh`

**Step 1: Write the test runner**

```bash
#!/usr/bin/env bash
# Test two Clive instances talking via SSH loopback.
# Usage: bash tests/test_loopback.sh
#
# Observe:
#   Terminal 1: this script (shows outer Clive output)
#   Terminal 2: tmux attach -t clive   (watch panes live)
#   Logs:       tail -f /tmp/clive/*/clive.log

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Clive Loopback Test ==="
echo ""
echo "To observe live, open another terminal and run:"
echo "  tmux attach -t clive"
echo ""
echo "Starting outer Clive with loopback profile..."
echo ""

python3 clive.py \
    --debug \
    -t loopback \
    --max-tokens 30000 \
    "read https://news.ycombinator.com and give me a summary on anthropic mythos"
```

**Step 2: Make executable**

Run: `chmod +x tests/test_loopback.sh`

**Step 3: Verify it parses**

Run: `bash -n tests/test_loopback.sh`
Expected: no output (syntax OK)

---

### Task 3: Run the test and observe

**Step 1: Start the test**

Run: `bash tests/test_loopback.sh`

**Step 2: In a second terminal, attach to tmux**

Run: `tmux attach -t clive`

Switch between panes with `Ctrl-b n` to see:
- **shell** pane: outer Clive's local shell
- **agent** pane: the SSH session running inner Clive

**Step 3: Watch logs**

Run: `tail -f /tmp/clive/*/clive.log`

Both instances write debug logs showing turns, LLM calls, screen reads, and protocol messages.
