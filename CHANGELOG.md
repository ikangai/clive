# Changelog

## 0.2.0 — Instance Dashboard & Local Addressing (2026-04-09)

### Added

- **Named instances** (`--name`) — Give a clive instance a name to make it addressable and long-lived. Named instances register in `~/.clive/instances/`, stay alive after their initial task, and accept follow-up tasks on stdin. Name collisions are rejected at startup.

- **Instance registry** (`registry.py`) — File-based registry at `~/.clive/instances/`, one JSON file per running instance. Automatic stale entry pruning via `os.kill(pid, 0)` liveness checks. No daemon, no socket, no coordination needed.

- **Local-first address resolution** — `clive@mybot` now checks the local instance registry before SSH. If a live, conversational instance matches, it resolves locally via tmux attach (microsecond latency). Local instances shadow remote hosts with the same name.

- **`--dashboard`** — Snapshot CLI showing all running instances, their PID, toolset, status, and uptime. Also shows remote agents from `~/.clive/agents.yaml`. Like `docker ps` for clive instances.

- **`--stop <name>`** — Send SIGTERM to a named instance by looking up its PID from the registry.

- **`/dashboard` TUI command** — Shows the same instance table in the TUI via `render_lines()`.

- **Conversational loop for named instances** — Named instances loop after task completion, reading additional tasks from stdin. Supports `/stop`, `exit`, `quit` to break the loop.

- **Conversational pane** (`session.py`) — Named instances get a dedicated `conversational` tmux window for receiving tasks from other instances.

- **Production hardening** — Sandboxing (bwrap/sandbox-exec/ulimit fallback), per-user resource quotas, file-based job queue with `fcntl.flock`, worker pool daemon with supervisor, health endpoint, cross-process SharedBrain via Unix domain sockets, agent-to-agent authentication, stall detection with exponential backoff.

---

## Agent Addressing & Peer Conversation (2026-04-08)

### Added

- **`clive@host` addressing** — Type `clive@devbox check disk usage` and Clive automatically resolves the address, opens an SSH pane, and routes the task. No profile or pane config needed. Multiple addresses supported: `clive@gpu render then clive@web upload`.

- **Agent registry** (`~/.clive/agents.yaml`) — Optional YAML registry for named agents with custom hosts, SSH keys, toolsets, and paths. Auto-resolve fallback when no registry entry exists.

- **TURN:/CONTEXT: conversation protocol** — Structured peer conversation between Clive instances. Inner Clive emits `TURN: thinking|waiting|done|failed`, `CONTEXT: {...}`, `QUESTION: "..."`, and `PROGRESS: ...` lines. Outer Clive reads turn state to decide when to act.

- **`--conversational` flag** — Enables conversational output mode for inner Clive instances. Auto-detected via `isatty()` when running over SSH (no TTY = conversational mode).

- **Turn-state-aware executor** — Agent panes now skip LLM calls during `TURN: thinking` (saving tokens), respond during `TURN: waiting`, and complete on `TURN: done/failed`. Backward compatible with legacy `DONE:` protocol.

- **Lazy pane injection** — Agent panes created on-demand when `clive@host` addresses are encountered. No need to pre-declare agent panes in toolset profiles.

- **BYOLLM via SSH** — API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`) forwarded to remote Clive via SSH `SendEnv`. Remote instance uses your keys — no keys stored on remote hosts.

- **Agent driver rewrite** (`drivers/agent.md`) — Updated for peer conversation protocol with TURN/CONTEXT/QUESTION handling rules.

### Removed

- **Loopback profile** — The `loopback` toolset profile and `localhost_agent` pane definition removed from `toolsets.py`. Replaced by `clive@localhost` addressing.

---

## Performance Optimizations (2026-04-07)

### Read Loop

- **Screen diffing** — Only changed lines sent to LLM after first turn. Uses `difflib.unified_diff` with 60-line cap. Cuts screen tokens by 60-80%.

- **Context compression** — Conversation history capped at 4 most recent turns. Prevents unbounded token growth in long interactive sessions.

- **Batched exit check** — Script execution and exit code capture combined into one tmux round-trip. Saves ~2 seconds per script attempt.

- **Expanded markers** — All shell-like panes (data, docs, media, browser, files) use marker-based completion detection. Eliminates 2-second idle timeout.

- **Scrollback capture** — `capture-pane -J -S-50` joins wrapped lines and includes recent scrollback. Agent sees output that scrolled off screen.

- **`wait` command** — Agent can explicitly pause and re-observe: `<cmd type="wait">3</cmd>`. Useful for long-running commands.

- **No-change early stop** — If screen is unchanged for 3 consecutive turns, subtask fails as stuck. Prevents wasting turns on stuck loops.

### Planning

- **Stronger script-mode push** — Planner prompt strongly prefers script mode (2.5x cheaper, equally reliable). Interactive only when observation is genuinely required.

---

## Gap Closure: Hardening + Full Layer Coverage (2026-04-07)

### Added

- **Layer 3 eval tasks** — 12 script quality tasks: correctness (rename, JSON sum, curl pipeline, Python parse, error handling), robustness (empty input, missing files, valid JSON, no-clobber), debug loop (syntax fix, wrong output, missing command).

- **Layer 4 eval tasks** — 10 planning quality tasks: DAG structure (parallel synthesis, dependency chains, minimal decomposition), mode assignment (script for batch ops, interactive for browsing, mixed modes).

- **Layer 1 eval tasks** — 4 end-to-end tasks testing full pipeline: TODO counting, API table formatting, log analysis, file inventory.

- **Data eval tasks** — 3 tasks exercising the data driver: CSV sum, CSV group-by, JSON transform.

- **Output format flags** — `--oneline` (single-line result), `--bool` (exit 0/1 for yes/no), `--json` (structured JSON output). All imply `--quiet`.

- **Streaming observation level** — Intervention detection during command execution (password prompts, confirmations, fatal errors). `mode: "streaming"` dispatches interactive loop with intervention detection.

- **Clive-to-clive protocol** — Agent driver prompt (`drivers/agent.md`) with DONE: JSON protocol. Executor parses DONE: lines on agent panes.

- **Script→interactive fallback** — Failed script subtasks automatically retry as interactive mode with increased turn budget.

- **Driver prompts** — 4 new drivers: data (jq/awk/mlr), docs (man/pandoc), email_cli (mutt state machine), media (ffmpeg/yt-dlp). Plus agent driver.

- **Evolution integration** — Evolution loop now includes Layer 3 tasks for harder selection pressure. `/evolve` slash command in TUI.

- **CI workflows** — Unit tests on every push, Layer 2 evals on push to main (with API key check).

- **Selfmod gate tests** — 17 unit tests for the deterministic safety gate (banned patterns, immutable files, tier approvals).

- **Script lifecycle** — Script mode writes `_result_{id}.json` and `_log_{id}.txt`. Script generation logged to audit trail.

- **Session management** — Session-scoped cleanup after run(). TUI uses session_dir.

- **Eval reliability** — /tmp/clive cleaned before each eval task. Baseline comparison via `--baseline` flag.

- **Mode validation** — Subtask.mode validated against known values (script/interactive/streaming). Unknown modes default to interactive with warning.

- **Pricing** — `pricing.json` with per-model rates. `EvalReport.estimated_cost()` for cost tracking.

---

## Phase 2: Observation Levels + Session Isolation (2026-04-07)

### Added

- **Script observation level** — Deterministic subtasks now bypass the turn loop. The planner assigns `mode: "script"` to tasks that can be solved with a single shell script. The executor generates the script in one LLM call, executes it, and checks the exit code. On failure, a repair loop reads the error and patches. ~2.5x cheaper on tokens than interactive mode.

- **Session-scoped filesystem** — Each run gets its own working directory at `/tmp/clive/{session_id}/`, preventing cross-run collisions. Session ID is displayed at startup.

- **Planner mode assignment** — The planner prompt now includes guidance for choosing between `script` and `interactive` observation levels. The plan display shows the assigned mode per subtask.

- **Script-mode eval tasks** — 5 new deterministic eval tasks (CSV filtering, log extraction, word counting, file listing, JSON creation) that exercise the script execution path.

### Eval results

| Suite | Tasks | Pass Rate | Tokens/task |
|---|---|---|---|
| Shell (interactive) | 5 | 100% | ~5,400 |
| Browser (interactive) | 5 | 100% | ~5,700 |
| Shell (script) | 5 | 100% | ~2,100 |

Total: 15 eval tasks, 14-15/15 passing (1 flaky due to cross-test contamination in shared `/tmp/clive/`).

---

## Phase 1: Sub-Agent Specialization + Layer 2 Evals (2026-03-16)

### Added

- **Output routing** (`output.py`) — `progress()` for telemetry (stderr in quiet mode), `result()` for final output (always stdout). Replaces bare `print()` calls.

- **`--quiet` / `-q` flag** — All telemetry to stderr, only the final result to stdout. Enables `clive` as a shell primitive: `result=$(clive -q "task")`.

- **Driver auto-discovery** — `drivers/*.md` files loaded automatically by `app_type`. Workers get tool-specific knowledge (keyboard shortcuts, command patterns, pitfalls) instead of a generic prompt.

- **Shell driver** (`drivers/shell.md`) — Compact reference card for bash: exit codes, patterns, quoting pitfalls.

- **Browser driver** (`drivers/browser.md`) — Reference card for lynx/curl/wget: page rendering, link extraction, API patterns.

- **Eval framework** — Isolated tmux fixtures (`session_fixture.py`), deterministic + cached LLM verifiers (`verifier.py`), metrics and reporting (`metrics.py`), CLI runner (`run_eval.py`).

- **10 Layer 2 eval tasks** — 5 shell tasks (find files, count patterns, word frequency, disk usage, JSON extraction) + 5 browser tasks (fetch page, extract links, JSON API, HTTP headers, multi-endpoint).

### Foundation (pre-Phase 1)

- tmux-based autonomous agent loop with plan → execute → summarize pipeline
- Parallel DAG execution across tmux panes with dependency tracking
- Composable toolset profiles (`-t standard+media+ai`)
- Multi-provider LLM support (OpenRouter, Anthropic, OpenAI, Gemini, LMStudio, Ollama)
- Textual-based TUI with slash commands
- Self-modification system with separation of powers (experimental)
- Remote habitat support via SSH with security layering
