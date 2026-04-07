# Phase 2: Observation Levels + Session Isolation

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add per-subtask observation granularity (`script` / `interactive`, with `streaming` as a future level) so deterministic subtasks bypass the turn loop. Introduce session-scoped filesystems for isolation.

**Architecture:** The tmux pane is a universal communication channel. The agent reads the screen, reasons, types — whether the other side is a shell, an app, another agent, or a remote machine. The key design variable is **how often the agent observes**:

| Level | Observation | When to use |
|---|---|---|
| `script` | Generate → execute → check exit code | Deterministic pipelines, file ops, known APIs |
| `interactive` | Full screen between each command (existing) | Multi-step exploration, unknown content, debugging |
| `streaming` (future) | Diffs during execution, can interrupt | Long-running ops, interactive apps, error recovery |

These aren't modes — they're points on a spectrum of observation frequency. The planner assigns the level; the executor dispatches accordingly. The same pane interface works for all levels.

**Extension points for the bigger picture:**
- **Pane-agents**: The executor dispatch is designed so each observation level is a standalone function. A future refactor replaces the thread pool with per-pane agent instances (each a clive instance).
- **Clive-to-clive**: `ssh remote "clive --quiet 'task'"` in a pane works today. Session-dir is pane-local, so remote panes get their own filesystem.
- **Streaming observation**: `wait_for_ready` gains optional intervention detection — cheap keyword matching during the wait loop, LLM escalation only when triggered.

**Tech Stack:** Python 3, libtmux, pytest

---

### Task 1: Add `mode` field to Subtask dataclass

Add `mode` to `Subtask` with default `"interactive"` for backward compatibility.

**Files:**
- Modify: `models.py:21-27`
- Create: `tests/test_models.py`

**Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
"""Tests for data models."""
from models import Subtask, Plan


def test_subtask_default_mode():
    s = Subtask(id="1", description="test", pane="shell")
    assert s.mode == "interactive"


def test_subtask_script_mode():
    s = Subtask(id="1", description="test", pane="shell", mode="script")
    assert s.mode == "script"


def test_plan_validates_with_mode():
    plan = Plan(task="test")
    plan.subtasks.append(Subtask(id="1", description="t", pane="shell", mode="script"))
    plan.subtasks.append(Subtask(id="2", description="t", pane="shell", mode="interactive", depends_on=["1"]))
    errors = plan.validate(valid_panes={"shell"})
    assert errors == []
```

**Step 2: Run test — expect FAIL**

Run: `python3 -m pytest tests/test_models.py -v`

**Step 3: Add mode field**

In `models.py`, add to Subtask after `max_turns`:

```python
    mode: str = "interactive"
```

**Step 4: Run test — expect PASS**

**Step 5: Commit**

```bash
git add models.py tests/test_models.py
git commit -m "feat: add mode field to Subtask (script/interactive observation levels)"
```

---

### Task 2: Wire mode through planner

Update planner prompt with observation-level guidance. Parse `mode` from planner JSON.

**Files:**
- Modify: `prompts.py:27-70`
- Modify: `planner.py:55-61`

**Step 1: Update planner prompt in prompts.py**

Replace `build_planner_prompt`. Add rule 10 for mode selection, update JSON example with mode field.

**Step 2: Parse mode in planner.py**

In the subtask parsing loop, add: `mode=s.get("mode", "interactive")`

**Step 3: Show mode in display_plan**

Add `[{s.mode}]` badge to the display line.

**Step 4: Run all tests — expect PASS**

**Step 5: Commit**

```bash
git add prompts.py planner.py
git commit -m "feat: planner assigns observation level (mode) per subtask"
```

---

### Task 3: Session-scoped filesystem

Generate session ID, create `/tmp/clive/{session_id}/`, propagate through pipeline.

**Files:**
- Modify: `session.py`
- Modify: `clive.py`
- Modify: `executor.py`
- Modify: `prompts.py`
- Create: `tests/test_session_dir.py`

**Step 1: Write failing test**

```python
"""Tests for session-scoped filesystem."""
import re
from session import generate_session_id

def test_session_id_format():
    sid = generate_session_id()
    assert re.match(r"^[a-z0-9]{8}$", sid)

def test_session_id_unique():
    ids = {generate_session_id() for _ in range(100)}
    assert len(ids) == 100
```

**Step 2: Implement generate_session_id in session.py**

**Step 3: Thread session_dir through setup_session → clive.py → execute_plan → run_subtask → build_worker_prompt**

All functions gain `session_dir: str = "/tmp/clive"` parameter. Default preserves backward compatibility.

**Step 4: Run all tests — expect PASS**

**Step 5: Commit**

```bash
git add session.py clive.py executor.py prompts.py tests/test_session_dir.py
git commit -m "feat: session-scoped filesystem (/tmp/clive/{session_id}/)"
```

---

### Task 4: Script observation level

Implement the script path: one LLM call → generate bash script → execute → check exit code → repair loop on failure.

**Files:**
- Modify: `executor.py`
- Modify: `prompts.py`
- Create: `tests/test_script_mode.py`

**Step 1: Write failing test for build_script_prompt**

**Step 2: Add build_script_prompt to prompts.py**

**Step 3: Add _extract_script and run_subtask_script to executor.py**

**Step 4: Branch in run_subtask: if mode == "script", dispatch to run_subtask_script**

**Step 5: Run all tests — expect PASS**

**Step 6: Commit**

```bash
git add executor.py prompts.py tests/test_script_mode.py
git commit -m "feat: script observation level — generate/execute/repair"
```

---

### Task 5: Update eval harness for mode + session_dir

**Files:**
- Modify: `evals/harness/run_eval.py`

Wire `mode` from task def into Subtask, pass `session_dir=ef.workdir` to run_subtask.

**Commit:**
```bash
git add evals/harness/run_eval.py
git commit -m "feat: eval harness supports mode and session_dir"
```

---

### Task 6: Script-mode eval tasks

5 deterministic tasks using `"mode": "script"`.

**Files:**
- Create: `evals/layer2/shell_script/tasks.json`
- Create: `evals/layer2/shell_script/fixtures/`

**Commit:**
```bash
git add evals/layer2/shell_script/
git commit -m "feat: add 5 script-mode eval tasks"
```

---

### Task 7: Full eval suite + baseline

Run all Layer 2 evals, save baseline, verify no regression.

```bash
python3 evals/harness/run_eval.py --layer 2 --output evals/baselines/2026-04-07-phase2.json
```

---

## Summary

| Component | Files | Purpose |
|---|---|---|
| Mode field | `models.py` | Observation level per subtask |
| Planner guidance | `prompts.py`, `planner.py` | LLM chooses script/interactive |
| Session filesystem | `session.py`, `clive.py` | `/tmp/clive/{session_id}/` isolation |
| Script execution | `executor.py`, `prompts.py` | Generate → execute → repair loop |
| Eval wiring | `evals/harness/run_eval.py` | Mode + session_dir support |
| Script evals | `evals/layer2/shell_script/` | 5 deterministic script-mode tasks |

**Design for the future:** Each observation level is a standalone function in executor.py. When pane-agents arrive, each function becomes an agent's core loop. When streaming arrives, it's a third function that diffs during wait_for_ready and escalates to the LLM on intervention signals. The pane interface is the same throughout.
