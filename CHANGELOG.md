# Changelog

## 0.4.0 — Observation Loop Efficiency (2026-04-14)

Six strategies that separate the observation loop into WAIT (free) / OBSERVE (cheap) / DECIDE (expensive) phases. The goal: the expensive main model is only called when genuine judgment is needed.

### Added

- **Per-pane model selection** — Drivers declare `agent_model` and `observation_model` tiers in frontmatter. Shell and data panes use cheap models (Haiku, Flash); browser and email use the default model. `runtime.resolve_model_tier()` maps tier labels (`fast`, `default`) to concrete model names per provider (7 providers supported).

- **Observation event system** (`observation.py`) — `ScreenClassifier` converts raw tmux screen captures into structured `ScreenEvent`s (SUCCESS / ERROR / NEEDS_INPUT / RUNNING / UNKNOWN) using regex. Each event carries a `needs_llm` flag — when False, the main model is not needed. Reuses `INTERVENTION_PATTERNS` from `completion.py` (no duplication). `format_event_for_llm()` produces compact messages like `[OK exit:0] file1.txt` instead of raw screen diffs.

- **Progressive context compression** (`context_compress.py`) — Replaces the bookend trim strategy (`_trim_messages`) with progressive compression. Old conversation turns are summarized by a cheap model into a running history, preserving information instead of dropping it. Falls back to bookend trimming when no observation model is configured. Compressor created once before the loop.

- **Observation-action decoupling** — After each command, the `ScreenClassifier` fires in the interactive runner. When exit_code==0 and `needs_llm==False`, a compact `[OK exit:0] summary` replaces the full screen diff in the next LLM context. The main model still decides the next action, but with 60-80% fewer input tokens.

- **Plan-Execute-Verify mode** (`planned_runner.py`) — New `"planned"` execution mode. The LLM generates a sequence of commands with verification criteria in ONE call (`build_planned_prompt`). The harness then executes each step mechanically — checking exit codes, handling retry/skip/abort — with zero additional LLM calls on the happy path. Added to `VALID_MODES`, dispatched in `executor.run_subtask()` before interactive mode, and included in the planner prompt.

- **Native tool-calling support** (`tool_defs.py`, `llm.chat_with_tools`) — Three pane operation tools: `run_command`, `read_screen`, `complete`. `chat_with_tools()` supports Anthropic (native tools param), OpenAI-compatible (auto-converts schemas), and DelegateClient (falls back to text mode). `parse_tool_calls()` normalizes both response formats into uniform `{name, args, id}` dicts.

- **Tool-calling interactive runner** (`toolcall_runner.py`) — Alternative to the text-based interactive runner. Uses native tool calls instead of regex command extraction. Key advantage: command batching — the model can emit multiple `run_command` calls in one response, each executed sequentially. Auto-detects provider support; falls back gracefully to text-based runner on unsupported providers or errors.

### Changed

- **Executor dispatch order** — `run_subtask()` now dispatches: skill > direct > script > planned > tool-calling interactive > text-based interactive. Tool-calling is attempted first for `interactive`/`streaming` modes when the provider supports it (OpenAI, Anthropic, OpenRouter, Gemini), with graceful fallback.

- **Driver frontmatter** — All 6 driver files now declare model tiers. Shell/data/media/docs: `agent_model: fast`, `observation_model: fast`. Browser/email: `agent_model: default`, `observation_model: fast`.

- **Planner prompt** — Mode guidance now includes all 5 modes (script, planned, interactive, streaming, direct) with clear selection criteria.

### Tests

- Full suite: 746 passing (+103 new). New test files: `test_pane_models.py` (12), `test_observation.py` (24), `test_context_compress.py` (10), `test_observation_decoupling.py` (4), `test_planned_runner.py` (14), `test_planned_integration.py` (4), `test_tool_calling.py` (18), `test_toolcall_runner.py` (11), `test_model_tiers.py` (17).

### Expected cost savings

| Strategy | Mechanism | Estimated savings |
|---|---|---|
| Per-pane models | Cheap models for shell/data tasks | 50-70% cost per pane |
| Observation classification | Skip full diff when command succeeds | 60-80% fewer input tokens per turn |
| Context compression | Summarize instead of drop old turns | 40-60% fewer input tokens |
| Plan-Execute-Verify | 1 LLM call for multi-step tasks | 80-90% fewer LLM calls |
| Tool-calling batching | Multiple commands per response | 30-50% fewer turns |
| Combined | All strategies | 3-5x overall reduction |

---

## 0.3.0 — BYOLLM delegation for remote clives (2026-04-13)

Remote `clive@host` addressing now works for local LLM providers (LMStudio, Ollama) without any network tunneling. The conversational protocol was rewritten from line prefixes to authenticated framed sentinels, closing a spoofing surface that was merely theoretical with cloud providers but load-bearing the moment inference is delegated.

### Added

- **Delegate LLM provider** — When the outer clive uses a local-only provider (LMStudio, Ollama), `build_agent_ssh_cmd` transparently sets `LLM_PROVIDER=delegate` on the remote. The remote's `DelegateClient` (`delegate_client.py`) serializes each inference call as a framed `llm_request` on stdout, blocks on stdin until a matching `llm_response` arrives. The outer's interactive runner detects the frame in the pane, calls its own local LLM, and types back an `llm_response` via `send_keys`. No tunneling, no `ssh -R`, no network changes on the remote.

- **Framed conversational protocol** (`protocol.py`) — Wire format `<<<CLIVE:{kind}:{nonce}:{base64(json(payload))}>>>`. Replaces the legacy `TURN:`/`CONTEXT:`/`QUESTION:`/`FILE:`/`PROGRESS:`/`DONE:` line prefixes. Base64 wrapping prevents stray tool output from ever matching a frame; the nonce slot adds authentication so a compromised inner LLM cannot forge state or request spurious inference.

- **Session nonce** — The outer generates a fresh 128-bit urlsafe nonce per agent session, injects it into the remote env as `CLIVE_FRAME_NONCE`, and stores it on the returned `pane_def`. Every frame the remote emits carries the nonce; every frame the outer parses is rejected unless the nonce matches.

- **Decoded agent-pane view** (`remote.render_agent_screen`) — The outer's interactive runner transforms the captured pane screen before handing it to the outer LLM: each valid frame becomes a human-readable pseudo-line (`⎇ CLIVE» turn=done`, `⎇ CLIVE» question: "..."`), forged or unauthenticated frames are silently dropped, raw `<<<CLIVE:...>>>` bytes never reach the LLM. The driver prompt (`drivers/agent.md`) describes the pseudo-line grammar as the source of truth.

- **`clive --agents-doctor`** (`agents_doctor.py`) — Pre-flight check that validates every host in `~/.clive/agents.yaml`: SSH reachability (BatchMode, 5s timeout), remote clive importability (honouring venv/versioned-python `path:` config), AcceptEnv coverage for every forwarded env var. Exits 0/1 so it composes into CI pipelines. Empty registry exits 0 with a helpful message.

- **SSH ControlMaster pooling** — `build_agent_ssh_cmd` emits `ControlMaster=auto`, `ControlPath=~/.clive/ssh/%C`, `ControlPersist=60s` for every agent connection. Delegate round-trips, scp file transfers, and reconnects attach to the existing channel in milliseconds instead of re-doing the full SSH handshake. Socket dir created lazily from `resolve_agent()` (covers all entry points) and degrades gracefully if the dir can't be created.

- **`LLM_BASE_URL` override + forwarding** — `llm.get_client()` honours `LLM_BASE_URL` as an override of the provider's default `base_url` for both the openai and anthropic paths (users running self-hosted proxies like LiteLLM). `agents._FORWARD_ENVS` gains `LLM_BASE_URL` and the previously-missing `GOOGLE_API_KEY`.

- **Conversational keepalive ticker** — Named instances with no initial task previously blocked on `stdin.readline()` with no outbound signal. A daemon thread now emits an `alive` frame every 15 seconds for the entire lifetime of the conversational block, so supervisors can distinguish a slow-but-working inner from a wedged one. Alive frames are filtered from the outer LLM's decoded view (supervisor signal only).

- **User documentation** (`docs/byollm-delegate.md`) — End-to-end guide covering cloud vs local provider paths, configuration cheat sheet, troubleshooting flowchart, threat model with a data-flow table, and a step-by-step manual smoke-test procedure against real LMStudio.

### Changed

- **Telemetry migration** — `progress()`, `step()`, `detail()`, `activity()` in `output.py` now emit framed `progress` frames when `_conversational` is active. Previously they emitted `PROGRESS: msg` line prefixes that the new parser couldn't see.

- **Interactive runner ordering** — For agent panes, the raw screen is passed to `executor.handle_agent_pane_frame()` first (to detect and answer `llm_request` frames), THEN rendered via `render_agent_screen()` for the outer LLM's view. Delegation side-channel traffic never consumes an outer-LLM turn.

- **`DelegateClient` timeout uses `select.select()`** — The 300-second chat-completion timeout now actually fires when the outer is silent. The initial implementation called `readline()` directly on stdin, which blocked indefinitely on a stuck outer, bypassing the deadline check entirely.

- **`clive.py` conversational loop** — Initial `sys.stdin.readline()` is skipped entirely when `keep_alive` is True, so control words (`exit`, `quit`, `/stop`) work on the first line the user sends, not just on subsequent ones.

### Removed

- **Legacy `TURN:`/`CONTEXT:`/`DONE:` line-prefix protocol** — Hard cutover, no compatibility shim. All conversational sessions are internal (clive-to-clive); no external consumers. `parse_remote_result` is gone from `remote.py`; regression test in `tests/test_remote.py` asserts it.

- **`server/conversational.py`** — Dead-code second emitter path that duplicated the framed protocol. Deleted along with its test.

### Security

- **Spoof-resistance.** The framed protocol's base64 wrapping prevents stray tool output from forming a valid frame (the marker characters `<`/`>`/`:` cannot appear inside base64). The per-session nonce prevents an adversarial LLM inside the inner — one that has been prompt-injected — from fabricating a valid frame, because the nonce is an env var and not part of any prompt the inner LLM can see. The `tests/test_protocol.py::test_decode_rejects_mismatched_nonce` test enforces the invariant.

- **Privacy of delegated prompts.** Under delegation, the remote's inner LLM prompts transit through the outer's LLM provider. If the outer is on LMStudio locally, nothing leaves your laptop. If the outer is on Anthropic or OpenAI, those providers receive the remote's inner prompts as if they were outer-originated. Document and data-flow table in `docs/byollm-delegate.md`.

### Tests

- Full suite: 593 passing. New: `tests/test_protocol.py` (17), `tests/test_delegate_client.py` (7), `tests/test_executor_delegate.py` (6), `tests/test_agents_doctor.py` (22), `tests/test_conversational_keepalive.py` (3), `tests/test_agent_view.py` (14), `tests/test_integration_delegate.py` (1 — end-to-end transport with mock LMStudio), `tests/test_llm_providers.py` (5).

---

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
