# Gap Closure: All 10 Gaps

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close all 10 gaps identified in the gap analysis — eval suites (L1/L3/L4/selfmod), output flags, streaming observation, clive-to-clive, CI, drivers, script lifecycle.

**Architecture:** Organized into 5 execution batches by dependency. Batches are parallel-safe internally. Each gap is one task. Eval tasks follow the existing `tasks.json` + `fixtures/` pattern. Code changes are tested and committed independently.

**Tech Stack:** Python 3, libtmux, pytest, GitHub Actions

---

## Batch 1: Quick Wins (parallel, no dependencies)

### Task 1: Output format flags — `--oneline`, `--bool`, `--json` (Gap 4)

**Files:**
- Modify: `clive.py` (add args, wire into run())
- Modify: `prompts.py` (update summarizer prompt per format)
- Create: `tests/test_output_flags.py`

Add three flags to argparse. Pass `output_format` to `run()` and `_summarize()`. The summarizer prompt adapts:
- `--oneline`: "Respond with a single line, no formatting."
- `--json`: "Respond with a JSON object: {\"result\": ..., \"status\": \"success\"|\"error\", \"subtasks\": [...]}"
- `--bool`: "Respond with exactly YES or NO." + clive exits 0 for YES, 1 for NO.

All three imply `--quiet`.

Tests: verify `_summarize()` produces correct format for each flag (mock LLM not needed — test prompt generation).

**Commit:** `feat: add --oneline, --bool, --json output format flags`

---

### Task 2: Script lifecycle — structured results + logs (Gap 10)

**Files:**
- Modify: `executor.py:run_subtask_script()` (~10 lines)

In `run_subtask_script`, after executing the script:
1. Capture stdout/stderr to `{session_dir}/_log_{subtask_id}.txt`
2. On success, write `{session_dir}/_result_{subtask_id}.json` with `{"status": "success", "summary": "..."}`

Feed the log file path into `_build_dependency_context()` so downstream subtasks can reference it.

**Commit:** `feat: script mode writes structured result.json and log files`

---

### Task 3: More driver prompts (Gap 9)

**Files:**
- Create: `drivers/data.md` (data processing pane — awk, jq, mlr, csvkit)
- Create: `drivers/docs.md` (documentation pane — man, tldr, pandoc)
- Create: `drivers/email_cli.md` (mutt/neomutt driver — state machine, keys)
- Create: `drivers/media.md` (ffmpeg, yt-dlp, imagemagick)

Each driver follows the compact reference card format (under 80 lines). Use the shell.md and browser.md as templates.

**Commit:** `feat: add data, docs, email_cli, media driver prompts`

---

## Batch 2: Eval Suites (parallel, no code dependencies)

### Task 4: Layer 3 evals — script quality (Gap 1)

**Files:**
- Create: `evals/layer3/script_correctness/tasks.json` (5 tasks)
- Create: `evals/layer3/script_correctness/fixtures/` (test data)
- Create: `evals/layer3/script_robustness/tasks.json` (4 tasks)
- Create: `evals/layer3/script_robustness/fixtures/`
- Create: `evals/layer3/debug_loop/tasks.json` (3 tasks)
- Create: `evals/layer3/debug_loop/fixtures/`

All use `"mode": "script"`. Deterministic verification. Fixtures include:
- `rename_files/`: 3 .txt files to rename to .bak
- `json_sum/`: multi-record JSON, verify computed sum
- `empty_input/`: empty file to test robustness
- `missing_file/`: task references nonexistent file
- `seeded_error/`: script with syntax error for agent to fix

**Commit:** `feat: add Layer 3 eval tasks (script correctness, robustness, debug loop)`

---

### Task 5: Layer 4 evals — planning quality (Gap 2)

**Files:**
- Create: `evals/layer4/planning/tasks.json` (5 tasks)
- Create: `evals/layer4/mode_assignment/tasks.json` (5 tasks)

These are different from Layer 2/3 — they test the **planner**, not the worker. The eval harness needs a small extension: call `create_plan()` and verify the DAG structure, not execute the plan.

Add `run_planning_eval()` to `run_eval.py` that:
1. Calls `create_plan()` with the task
2. Verifies DAG structure via LLM verifier (cached)
3. Checks mode assignments against expected

**Commit:** `feat: add Layer 4 eval tasks (planning quality, mode assignment)`

---

### Task 6: Selfmod eval suite (Gap 8)

**Files:**
- Create: `tests/test_selfmod_gate.py` (unit tests for gate — faster than full eval)

These are best implemented as pytest unit tests rather than eval tasks, since they test the gate's regex patterns directly:
- `eval()` in proposed code → rejected
- `os.system()` → rejected
- `shell=True` → rejected
- `__import__()` → rejected
- IMMUTABLE file modification → rejected
- GOVERNANCE file with insufficient approvals → rejected
- Clean OPEN-tier change → accepted
- Audit trail written for every attempt
- Hash chain integrity after multiple attempts

**Commit:** `feat: add selfmod gate and pipeline unit tests`

---

## Batch 3: Higher-layer evals + CI (parallel)

### Task 7: Layer 1 evals — end-to-end (Gap 3)

**Files:**
- Create: `evals/layer1/end_to_end/tasks.json` (5 tasks)
- Create: `evals/layer1/end_to_end/fixtures/`

These test the FULL pipeline (plan + execute + summarize). They're expensive but use the same harness. Each task needs:
- A fixture directory
- A task description that requires planning (not just one command)
- LLM-based verification (cached) since output is natural language

Tasks from SPEC:
1. Count Python files with TODO comments
2. Fetch JSONPlaceholder API, format as table
3. Find most recent error in a log file
4. Multi-step: analyze + summarize

**Commit:** `feat: add Layer 1 end-to-end eval tasks`

---

### Task 8: CI integration (Gap 7)

**Files:**
- Create: `.github/workflows/test.yml` (unit tests on every push)
- Create: `.github/workflows/eval.yml` (Layer 2 evals on push, with tmux)

The test workflow runs `python3 -m pytest tests/ -v`. The eval workflow needs tmux installed in CI.

GitHub Actions config:
```yaml
- name: Install tmux
  run: sudo apt-get install -y tmux
- name: Start tmux server
  run: tmux start-server
- name: Run evals
  run: python3 evals/harness/run_eval.py --layer 2 --ci --baseline evals/baselines/latest.json
```

**Commit:** `ci: add unit test and eval workflows`

---

## Batch 4: Architectural features

### Task 9: Streaming observation level (Gap 5)

**Files:**
- Modify: `completion.py` (add intervention detection)
- Modify: `executor.py` (add streaming observation dispatch)
- Modify: `models.py` (validate mode values)

Add `INTERVENTION_PATTERNS` to `completion.py`:
```python
INTERVENTION_PATTERNS = [
    r'\[y/N\]', r'\[Y/n\]', r'Continue\?', r'password:', r'Password:',
    r'Are you sure', r'Overwrite', r'Press .* to continue',
    r'ERROR:', r'FATAL:', r'Permission denied',
]
```

In `wait_for_ready`, when an intervention pattern is detected during idle wait, return early with method `"intervention"` and the matched pattern.

In `executor.py`, add `run_subtask_streaming()` that:
1. Sends a command
2. Calls `wait_for_ready` with intervention detection
3. If intervention detected → escalate to LLM for decision
4. If completion detected → proceed normally

The planner can assign `mode: "streaming"` for long-running or interactive tasks.

**Commit:** `feat: add streaming observation level with intervention detection`

---

### Task 10: Clive-to-clive protocol (Gap 6)

**Files:**
- Create: `drivers/agent.md` (driver for communicating with inner clive)
- Modify: `toolsets.py` (add agent pane type)
- Modify: `executor.py` (parse DONE: protocol in interactive mode)

The `agent` driver prompt teaches the outer agent:
```markdown
# Agent Driver (clive-to-clive)

PROTOCOL:
  Send tasks as plain text. Wait for DONE: line.
  DONE: {"status": "success", "result": "..."} — task completed
  DONE: {"status": "error", "reason": "..."} — task failed

USAGE:
  Type task description, press Enter, wait for DONE: line.
  Parse the JSON after DONE: for structured results.
```

Add `app_type: "agent"` to toolsets with:
```python
{
    "name": "remote_agent",
    "cmd": "ssh {host} 'python3 clive.py --quiet'",
    "app_type": "agent",
    "description": "Remote clive instance",
}
```

The executor's interactive mode already reads the screen — parsing DONE: is just a check in `parse_command` or `run_subtask` that detects structured output.

**Commit:** `feat: add clive-to-clive agent driver and protocol`

---

## Execution order

```
Batch 1 (parallel): Task 1, 2, 3     — quick wins
Batch 2 (parallel): Task 4, 5, 6     — eval suites
Batch 3 (parallel): Task 7, 8        — L1 evals + CI
Batch 4 (sequential): Task 9, 10     — architectural
```

Each batch commits independently. Total: 10 commits.
