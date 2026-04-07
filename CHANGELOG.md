# Changelog

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
