# clive — Architecture Spec v3

## What clive is

clive is an LLM agent runtime where the terminal is the execution environment. The agent reads the tmux screen, reasons, types commands. No APIs, no function schemas, no tool registries. The pane IS the interface.

```
NOT:   agent → [tool registry] → API call → result
BUT:   agent → [terminal] → observe screen → reason → type keys → observe result
```

clive is simultaneously:
- A **compiler**: natural language → keystrokes/scripts
- A **runtime**: coordinates per-pane sub-agents across a DAG
- A **primitive**: callable from shell scripts (`result=$(clive -q "task")`)
- A **substrate**: clive instances communicate via SSH, evolve their own prompts, schedule themselves via cron

---

## Governing Principle

**LLM where judgment is required, shell everywhere else.**

If a task is "count lines in file.txt," it should not enter a turn loop. Generate the script, execute, check exit code, done — one LLM call. The turn loop engages only when observation and adaptation are genuinely required.

The system enforces this through observation levels, plan compilation, planner bypass, and a boldness prompt that teaches the agent to pipeline predictable sequences in a single response.

---

## The Observation Loop

The core of clive is a tmux observation loop optimized across 49 dimensions:

```
OBSERVE:  capture_pane(-J -S-50) → strip blanks → screen_diff → progress signals
THINK:    stream LLM response → detect </cmd> early → act before response completes
ACT:      safety check → pipeline N commands → wrap with EXIT:$? marker
WAIT:     adaptive poll (10ms→500ms backoff) → intervention detection → marker/prompt/idle
FEEDBACK: command echo + exit code + timing + outcome signal + auto-verify + recovery sharing
```

### Observation Levels

The planner assigns per subtask. The executor dispatches accordingly:

| Level | What happens | When to use | Typical LLM calls |
|---|---|---|---|
| `script` | Generate script → execute → check exit → repair loop | Deterministic: file ops, APIs, data processing | 1 (happy path) |
| `interactive` | Turn-by-turn: observe → think → act → wait → feedback | Exploration, debugging, interactive apps | 2-8 |
| `streaming` | Like interactive but with intervention detection during wait | Long-running commands, password prompts | 2-8 |

### Command Pipelining

The LLM can generate multiple commands in one response. The executor runs them sequentially, checking exit codes between each. If any fails (non-zero exit), the pipeline stops and the LLM sees the error. For predictable sequences like `mkdir → write → verify → task_complete`, this completes the task in one LLM call.

### Plan-to-Script Compilation

Sequential all-script same-pane DAGs are automatically collapsed into a single subtask:

```
[1:script:shell] extract → [2:script:shell] filter → [3:script:shell] report
                                    ↓ compiled to ↓
[compiled:script:shell] "Step 1: extract. Step 2: filter. Step 3: report."
```

Three LLM calls become one. The planner bypass adds another layer: trivial tasks (`ls`, `grep`, `curl`) skip the planner entirely — direct to script mode with zero planning overhead.

### Planner Bypass Hierarchy

```
Task arrives
  ↓
  Is it trivial? (ls, grep, curl, <20 words, single pane)
    → YES: skip planner, direct script mode (1 LLM call total)
  ↓
  Is there a cached plan? (session log, >60% word overlap with previous success)
    → YES: reuse plan shape, skip planner (0 planner LLM calls)
  ↓
  Plan with cost awareness (budget hint in planner prompt)
    → Collapse if all-script linear chain
    → Execute
```

### Completion Detection

Three strategies, checked in priority order:

1. **Marker**: `command; echo "EXIT:$? ___DONE_marker___"` — deterministic, fast (10-20ms)
2. **Prompt sentinel**: `[AGENT_READY] $` on last line — works for any shell
3. **Idle timeout**: screen unchanged for N seconds — universal fallback

Adaptive polling: 10ms initial → doubles to 500ms cap. Resets to 10ms on screen change.

### Screen Diffing

Only changed lines are sent to the LLM after turn 1. The diff is annotated:

```
[Screen update: +3 -0 lines — minimal change]
  file1.txt
  file2.txt
  [AGENT_READY] $
```

Capped at 60 lines to prevent context bloat. Full screen on first turn and when >50% changed.

### Command Feedback

After every shell command, the agent receives:

```
[Command executed: curl -s api.example.com]
[Exit: 0 | 1.3s | marker]
[Outcome: success indicators detected]
[Verified: response.json exists, 2048 bytes, valid JSON]
```

Auto-verification checks file writes automatically. Exit codes are deterministic (not regex guessing). Timing helps the agent reason about retries and timeouts.

### Context Management

- **Bookend trimming**: keeps first turn (initial context) + last 3 turns. Prevents unbounded growth while preserving critical early state.
- **Progressive prompt thinning**: after turn 1, system prompt shrinks from ~600 tokens to ~40 tokens (rules reminder only). Full prompt cached via Anthropic's `cache_control`.
- **Budget awareness**: `[Budget: 8,000 tokens remaining]` shown when <50% remains. Agent self-regulates.

---

## Agent Architecture

### Per-Pane Sub-Agents (PaneAgent)

Each tmux pane gets its own `PaneAgent` — a persistent wrapper that maintains context across subtasks:

```python
PaneAgent("shell")
  ↓ executes subtask 1 → remembers output
  ↓ executes subtask 3 → sees pane history from subtask 1
  ↓ tracks total tokens, completed subtasks
```

Benefits over the stateless thread pool model:
- Context carries forward between sequential subtasks on the same pane
- Each agent tracks its own token usage
- Natural boundary for future model heterogeneity (different models per pane)

### Plan Context

Each agent knows its role in the larger plan:

```
[Plan: "analyze sales data" — subtask 1 of 3]
[Parallel: 2:browser is fetching reference data]
[Downstream: subtask 3 needs your output for report generation]
[Pane: shell [shell], 80x24, session_dir=/tmp/clive/a1b2c3d4]
```

~50 tokens of strategic awareness. The agent knows the goal, its role, what's running in parallel, and who depends on its output.

### Inter-Agent Communication

| Channel | When | What |
|---|---|---|
| Dependency context | Subtask start | Semantic summary + file schema from completed deps |
| Shared scratchpad | Every turn | `_scratchpad.jsonl` — real-time notes between parallel agents |
| Cross-pane peek | On demand | `<cmd type="peek" pane="browser">` reads another pane's screen |
| File registry | After completion | Auto-inspected output files with type/schema/preview |
| Recovery patterns | After error recovery | "Agent 1 fixed 403 by adding auth header" → scratchpad |
| Pane state handoff | Between sequential subtasks | Previous subtask's screen as initial context |

### Output Sniffing

After a subtask completes, the executor automatically inspects output files:

```
Dependencies completed:
  [1] DONE: Extracted 47 rows → data.json
  Available files:
    data.json — json_array, 47 items, keys: name, amount, city
    summary.txt — text, 3 lines
```

Zero LLM calls — just `json.load()` and `csv.reader()` on the first few bytes.

---

## DAG Scheduler

Event-driven with per-pane locking:

- **Instant dispatch**: `threading.Event` wakes the scheduler immediately when a future completes (no 0.5s polling)
- **Branch cancellation**: when a subtask fails, running subtasks whose results are now useless are cancelled
- **Script→interactive fallback**: failed script subtasks automatically retry as interactive with increased turn budget
- **Token budget enforcement**: `--max-tokens` aborts execution when cumulative tokens exceed the budget
- **Replanning on failure**: when subtasks fail with skipped dependents, the planner is called again with failure context

---

## Skills System

Skills are procedural recipes — multi-step procedures that inject into the worker prompt alongside the driver. Drivers are static knowledge ("how bash works"). Skills are dynamic procedures ("how to analyze logs").

```
drivers/shell.md  → "KEYS: ctrl-c=interrupt, EXIT CODES: 0=success..."
skills/analyze-logs.md → "1. Count ERRORs. 2. Extract unique messages. 3. Find patterns..."
```

Invocation: `clive "check the server [skill:analyze-logs]"`

Discovery: `clive --list-skills`

Drop a `.md` file in `skills/` to add a new skill. No registry to update.

---

## Execution Modes

### CLI

```bash
clive "your task"                          # default
clive -t standard "browse and analyze"     # with browser + data panes
clive -q "count files"                     # quiet: result to stdout only
clive --json "list TODOs"                  # structured JSON output
clive --bool "is the server running?"      # exit 0=yes, 1=no
clive --oneline "summarize this log"       # single-line result
```

### TUI

```bash
clive --tui
```

Interactive terminal UI with slash commands: `/profile`, `/evolve`, `/selfmod`, `/status`, `/cancel`.

### Shell Primitive

```bash
result=$(clive -q "task")
if clive --bool "is disk usage > 80%?"; then alert_oncall; fi
clive -q --json "list all TODOs" | jq '.[] | select(.priority=="high")'
```

### Scheduled

```bash
clive --schedule "0 * * * *" "check disk usage"    # hourly
clive --list-schedules
clive --history check_disk_usage
```

Results persist in `~/.clive/results/`. Crontab entries auto-managed.

### Cross-Machine

```
laptop
  └── clive (orchestrator)
        ├── pane: shell (local)
        ├── pane: browser (local)
        └── pane: agent (ssh → server → clive --quiet)
                  └── receives task, executes, returns DONE: JSON
```

The outer clive sends natural language. The inner clive executes and returns via `DONE: {"status": "success", "result": "..."}`. The same pane interface for local and remote execution.

---

## Evolution Framework

Driver prompts evolve against the eval suite:

```bash
python3 evolve.py shell --variants 3 --generations 2
```

1. **Mutate**: 3 strategies (token optimizer, turn optimizer, robustness optimizer) generate variant prompts
2. **Evaluate**: each variant runs the eval suite twice (conservative: take minimum score)
3. **Select**: highest fitness wins, but only if it beats baseline pass rate
4. **Lineage**: `drivers/history/{driver}_gen{N}_{score}.md` tracks evolution

Fitness: `0.5×pass_rate + 0.3×turn_efficiency + 0.2×token_efficiency`

Hard constraint: pass rate can never drop below baseline.

---

## Self-Modification

Experimental. Enabled via `CLIVE_EXPERIMENTAL_SELFMOD=1`.

```
Goal → Proposer(LLM) → Reviewer(LLM) → Auditor(LLM) → Gate(regex) → Snapshot(git) → Apply
```

- **Deterministic gate**: regex-based, cannot be "talked past." Rejects `eval()`, `os.system()`, `shell=True`, network access in selfmod modules
- **Immutable anchor**: `gate.py` and `constitution.md` cannot be modified
- **Five-tier file classification**: IMMUTABLE → GOVERNANCE → CORE → STANDARD → OPEN
- **Audit trail**: append-only, hash-chained JSON in `.clive/audit/`
- **Rollback**: git snapshots, `--undo` to revert

---

## Eval Framework

44 tasks across 4 layers:

| Layer | Tasks | What it tests |
|---|---|---|
| Layer 1 | 4 | End-to-end: plan + execute + summarize |
| Layer 2 | 18 | CLI tool operation: shell, browser, script, data |
| Layer 3 | 12 | Script quality: correctness, robustness, debug loop |
| Layer 4 | 10 | Planning: DAG structure, mode assignment |

Plus 17 selfmod gate unit tests.

```bash
python3 evals/harness/run_eval.py --layer 2          # run Layer 2
python3 evals/harness/run_eval.py --all               # run everything
python3 evals/harness/run_eval.py --compare model1 model2  # A/B test models
python3 evals/harness/run_eval.py --baseline latest.json --ci  # CI regression check
```

---

## Safety

### Command Blocklist

Before any command reaches the pane:

```python
BLOCKED_COMMANDS = [rm -rf /, shutdown, reboot, mkfs, dd of=/dev/, fork bombs]
```

Deterministic regex. Cannot be bypassed by the LLM.

### Token Budget

`--max-tokens 50000` (default). Execution aborts when exceeded. Remaining subtasks marked SKIPPED.

### Graceful Shutdown

SIGINT/SIGTERM handler kills tmux sessions, cleans session directories, exits cleanly.

---

## File Map

```
clive.py          — orchestrator: bypass → plan → compile → execute → summarize
executor.py       — DAG scheduler, observation loop, command pipeline, feedback
pane_agent.py     — per-pane persistent agent with context continuity
planner.py        — LLM task decomposition into DAG
session.py        — tmux session/pane management, session ID
completion.py     — adaptive polling, marker/prompt/idle detection, intervention
screen_diff.py    — diff-only screen updates with progress signals
file_inspect.py   — auto-detect JSON schema, CSV columns, file types
prompts.py        — planner, worker, script, triage, summarizer prompts
skills.py         — procedural recipe loader + discovery
scheduler.py      — cron scheduling with result persistence
evolve.py         — evolutionary driver prompt optimization
tool_schemas.py   — JSON schemas for future tool_use integration
models.py         — Subtask (mode, _retried), Plan, SubtaskResult, PaneInfo
toolsets.py       — 3-surface registry: panes, commands, endpoints
llm.py            — multi-provider client (Anthropic, OpenAI, Gemini, local) + streaming
output.py         — quiet mode routing (telemetry→stderr, result→stdout)
tui.py            — Textual terminal UI
selfmod/          — self-modification pipeline with deterministic gate
drivers/          — per-tool reference cards (shell, browser, data, docs, email, media, agent)
skills/           — procedural recipes (analyze-logs, api-test, backup, file-organize, git-summary)
evals/            — 44 eval tasks + harness + baselines
tests/            — 167 unit tests
```

---

## Design Decisions (closed)

| Decision | Choice | Why |
|---|---|---|
| Script persistence | Delete on session end | Unmanaged library becomes noise |
| Sub-agent state | Per-pane PaneAgent | Context continuity > fresh start |
| Coordinator → agent | Natural language | Descriptions benefit from flexibility |
| Agent → coordinator | Structured result + file schema | Results need to be parseable |
| Observation default | Script mode | 2.5x cheaper, equally reliable |
| Command protocol | XML tags (tool_use schemas ready) | Works across all providers |
| Completion detection | Marker > prompt > idle | Fast, then reliable, then universal |
| Screen updates | Diff only | 60-80% token savings after turn 1 |
| Context management | Bookend (first + last 3 turns) | Preserves initial state + recent |
| Plan compilation | Collapse linear script chains | 67% fewer LLM calls for pipelines |
| Inter-agent comm | Filesystem + scratchpad + peek | No new protocol, tmux-native |
| Evolution selection | 2 runs, take minimum | Conservative against stochasticity |
| MCP vs CLI | CLI as default, MCP as future option | Speed and familiarity for inner loop |
