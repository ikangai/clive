# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Clive is an **environment-interface** agent: an LLM drives CLI tools through tmux by reading the terminal screen and typing keystrokes. No MCP, no tool schemas, no REST. The terminal *is* the interface. Any LLM provider can orchestrate; Claude (via `tools/claude.sh`) is just one tool in the toolset.

Core philosophy: **LLM where judgment is required, shell everywhere else.**

## Common commands

```bash
# Run (from repo root — clive.py is a wrapper that adds src/clive/ to sys.path)
python clive.py "task"                         # minimal toolset, default
python clive.py -t standard "task"             # +browser, data, docs
python clive.py --tui                          # Textual TUI
python clive.py --list-toolsets
python clive.py --list-tools
python clive.py -q "task"                      # quiet: result to stdout, telemetry to stderr

# Tests (pytest; conftest.py injects src/clive/ onto sys.path for flat imports)
pytest                                          # all 700+ tests
pytest tests/test_executor.py                   # single file
pytest tests/test_executor.py::test_name        # single test
pytest -k pattern                               # by name pattern

# Evals (layered: L1 unit-ish → L4 end-to-end)
python evals/harness/run_eval.py --layer 2
python evals/harness/run_eval.py --layer 2 --tool shell

# Watch the agent work live
tmux attach -t clive
```

Environment: **no virtualenv is active**. Install deps via `pip install -r requirements.txt --break-system-packages` (PEP 668). Python 3.10+ and `tmux` required.

## Architecture: plan → execute → summarize

1. **Planner** (`src/clive/planning/planner.py`) — LLM decomposes the task into a **Subtask DAG** (JSON). Each subtask gets an execution `mode` and is assigned to a pane.
2. **DAG scheduler** (`planning/dag_scheduler.py`) — runs independent subtasks in parallel on different tmux panes; dependent ones wait. Each subtask has isolated working dir `/tmp/clive/{session_id}/`.
3. **Executor** (`execution/executor.py`) dispatches by mode to a mode-specific runner.
4. **Summarizer** (`planning/summarizer.py`) — synthesizes results.

### Execution modes (key design axis)

Subtasks carry a `mode` field. Pick the cheapest mode that works — the planner defaults to `script`/`planned` when deterministic.

| Mode | Runner | LLM calls | Use when |
|---|---|---|---|
| `direct` | `executor.py` | 0 | Classifier already recognized a literal command |
| `script` | `script_runner.py` | 1 (+repairs) | Deterministic single-step; generate → exec → check exit → repair on failure |
| `planned` | `planned_runner.py` | 1 | Deterministic multi-step; all commands generated up front |
| `interactive` | `interactive_runner.py` | N turns | Read-think-type loop; needs observation |
| `streaming` | (interactive w/ intervention) | N turns | Long-running, password prompts, confirmations |
| tool-calling | `toolcall_runner.py` | N turns | Provider supports native tools (`run_command`, `read_screen`, `complete`); enables batching |

### Observation loop (interactive modes)

Three phases by cost — WAIT (free: markers, polling, exit codes) → OBSERVE (cheap: `observation/observation.py` regex `ScreenClassifier` → compact events like `[OK exit:0] ...`) → DECIDE (expensive: main model, only when classifier escalates). This cuts tokens 60-80%. `observation/context_compress.py` runs a cheap model to progressively compress old turns rather than dropping them. Under token pressure (≥70% of the subtask `token_budget`, threaded from the plan-level `max_tokens`), `maybe_squash` compresses everything but the last 2 turns into one summary — max 2 squashes per subtask, never before turn 5, emits a `squash` event (gh#6).

### Per-pane models

Each pane declares a model tier (`fast`/`default`) via its driver frontmatter. Shell/data use Haiku/Flash; browser/email use the default. The tier label resolves to a concrete model via the active provider (`session/session.py`, `llm/llm.py`).

### Pane border state colors (gh#4)

`session/pane_state.py` — when attached to the tmux session, each pane's border color signals agent state: working=yellow, done=green, failed=red, idle/skipped=grey. `PaneBorderColorizer(panes)` adapts the execution `on_event` protocol (seeding a subtask-id → pane map from `subtask_start`, recoloring via `select-pane -P`); `clive_core.py` composes it with the progress printer through `chain_on_event`. Best-effort — every tmux call is swallowed so it never disrupts a run. Opt out with `CLIVE_PANE_COLORS=0`.

### Command protocol (non-tool-calling path)

Plain text. LLM emits ```bash fenced blocks; `observation/command_extract.py` parses them. Completion signal is literally `DONE: <summary>` — **not XML**. This was an explicit simplification ("Pane Core Refocus", 2026-04-09) — do not reintroduce XML or a PaneAgent/SharedBrain abstraction.

### Drivers

`drivers/*.md` are auto-discovered per `app_type` (shell, browser, email, ...). Compact reference-card format with frontmatter for model tier. The driver prompt is the persona the LLM adopts inside that pane. The "RESPONSE FORMAT" section of a driver is the highest-leverage part — see `memory/project_autoresearch_driver_findings.md`.

### Toolsets

`session/toolsets.py` — profiles (`minimal`, `standard`, `full`, `remote`, ...) composed of categories via `+` syntax (e.g. `-t standard+media+ai`). `resolve_toolset()` returns panes, commands, endpoints. Toolsets are **dynamic**: categories expand at runtime via `_expand_toolset()`. `commands.py:check_commands()` verifies installed CLIs at startup.

**Four-tier progressive disclosure (gh#39, opt-in via `CLIVE_PROGRESSIVE_TOOLS=1`):**
- Tier 0 = `build_tier0_summary(categories)` — category index with counts (~100 tokens)
- Tier 1 = `build_tier1_names(categories)` — tool names per category (~50 tokens/cat)
- Tier 2 = `build_tier2_card(name)` — compact per-tool reference card (≤200 chars)
- Tier 3 = `drivers/*.md` (unchanged)

Under the flag, `build_tools_summary` emits Tier 0 + Tier 1 instead of the legacy flat dump (~86% token reduction on `full`). Planner emits `subtask.tools=[...]`; interactive/toolcall runners inject matching Tier-2 cards via `build_worker_tool_context`. In-pane discovery: `tools/clive-tools list|info`. Auto-categorization helper: `classify_tool_to_category(name, description)` (used by gh#41 auto-explore to drop newly-explored tools into an existing category). The flag is default-off until validated by gh#40 evals; legacy path is untouched.

### Response isolation (gh#14, opt-in via `CLIVE_PANE_ISOLATION=1`)

`execution/pane_isolation.py` — per-pane locks normally serialize whole subtasks (send+execute+wait). Under the flag, shell-like panes isolate each request's *output* instead: `wrap_isolated` bookends commands with unique tags inside a subshell `( ... )` (no env/cwd leakage between subtasks), and `PaneIsolation.submit()/feed()` demuxes a pane's line stream to per-request Futures — the lock covers only `send_keys`. `run_subtask_direct` uses it today (waits on its own exit-code file, lock shrinks to send-only); the `PaneIsolation` demux class is the building block for gh#12's control-mode sidecar. Caveats: type-ahead assumes commands don't read stdin; TUI panes keep whole-pane locking.

### Exit-code in PS1 (gh#8, opt-in via `CLIVE_PS1_EXITCODE=1`)

`observation/ps1_exit.py` — alternative to the `EXIT:`/`___DONE___` command wrapper (`observation/completion.py:wrap_command`, the default, left unchanged). Under the flag, `agent_ready_prompt_setup()` installs a `PROMPT_COMMAND` that captures `$?` and a PS1 that renders `[AGENT_READY] ec=<n> $`, so the prompt line itself carries the last exit code — completion detection no longer depends on wrapping the command (tradeoff: couples to shell prompt config). The literal `[AGENT_READY]` substring is preserved so `session.check_health` and plain-prompt detection still match; `completion.py` Strategy 2 additively recognizes the `ec=` form and `parse_ps1_exit()` recovers the code. Ships the *mechanism* only — re-wiring runners to drop `wrap_command` is a deferred follow-up. `session/session.py` routes all four prompt-setup sites through the helper (byte-identical when the flag is off).

### Router (3-tier intent classification)

`src/clive/router.py` routes user input through: (1) direct match → (2) cheap classifier → (3) full planner. Most tasks resolve at tier 1 or 2.

## Source layout (src/clive/)

Flat imports work because `conftest.py` and `clive.py` put `src/clive/` on `sys.path`. Subpackages are organizational, not import-path scoping: inside `src/clive/` modules import each other flat (`from models import Subtask`), not via dotted paths.

- `llm/` — provider-agnostic client, prompts, native tool defs, delegate client
- `planning/` — planner, DAG scheduler, summarizer
- `execution/` — mode-specific runners (script, planned, interactive, toolcall, skill) + shared `runtime.py`
- `observation/` — screen classifier, diff, compression, completion detection, command extraction; `control_sidecar.py` (gh#12) attaches one read-only `tmux -C` client per session and fans %output/%window-close events to subscribers — wired into `dag_scheduler` wake-ups behind `CLIVE_CONTROL_SIDECAR=1` (default off; falls back to the 0.5s poll)
- `session/` — tmux session/pane management, toolsets, command checks
- `networking/` — `clive@host` addressing, framed protocol, instance registry, dashboard, SSH/SCP
- `tui/` — Textual TUI + slash commands
- `selfmod/` — experimental self-modification (governed by `.clive/constitution.md`, guarded by `gate.py` regex)
- `discovery/` — self-learning tool discovery (gh#41). `explore_tool(name)` adapts `run_subtask_interactive` to run bounded probes (`--help`/`-h`/`man`/`tldr`) against an unknown CLI in a fresh exploration pane; `generate_driver(name, result)` synthesizes a `drivers/<name>.md` from the exploration log; `write_generated_driver(name, text)` writes atomically. Manual entry: `clive --explore <tool>`. Auto-gen header is injected INSIDE the body (after the frontmatter close) so `_parse_driver_frontmatter` still parses metadata at byte 0. Phase 3 (`refiner.py`): `refine_driver(name, signals)` re-synthesizes a driver from Layer 5 eval failures — `RefinementSignal.from_eval_result` duck-types `ToolEvalResult` so `src/clive/` never imports `evals/`; refined text returns to the caller for the normal quarantine write path (it is not auto-promoted).

  **Quarantine flow (gh#41 scenario #50):** `write_generated_driver` defaults to `drivers/.unreviewed/<name>.md`, NOT `drivers/<name>.md`. `load_driver` does not search the `.unreviewed/` subdir, so a fresh auto-gen driver is non-loadable until promoted. To activate: `clive --promote-driver <tool>` (or call `promote_driver(name)` programmatically) — atomically moves the file from `.unreviewed/` to the canonical location after re-validating the content. `CLIVE_TRUST_UNREVIEWED=1` env var opens an escape hatch for evals/CI; reviewed drivers always win over unreviewed copies. Hand-written drivers in `drivers/` are unaffected.

  Safety invariants (post-audit, gh#41 debug pass):
  - Tool names are validated by `_check_tool_name` at the **top** of `handle_explore`, `explore_tool`, and `write_generated_driver` — regex `^[a-z][a-z0-9_-]*$` (lowercase only, no dots — closes APFS case-collision and `foo.md`-style confusion) **plus** a `RESERVED_NAMES` set that refuses to overwrite core hand-written drivers (`explore`, `shell`, `browser`, `data`, `docs`, `default`, `email`, `email_cli`, `agent`, `media`, `room`) even with `--explore-overwrite`. An invalid name fails fast — no LLM tokens spent, no pane opened.
  - Driver writes are atomic: non-overwrite uses `open("x")` (O_EXCL); overwrite uses write-tmp + `os.replace`. No TOCTOU window across concurrent invocations.
  - `_check_exploration_safety` reuses the shared `runtime._strip_sudo_and_env` helper (POSIX env-var-name regex `^[A-Za-z_][A-Za-z0-9_]*$`) so `_=x cmd`-style env prefixes can't bypass the CREDENTIAL_TOOLS / INTERACTIVE_TOOLS deny lists. The same helper closes the equivalent bypass in `_check_command_safety` (the base safety gate).
  - `_check_command_safety` blocks `curl|bash` / `wget|sh` / `eval-curl-subst` / `base64 -d | sh` pipelines in addition to the pre-existing `rm -rf /`, `dd of=/dev/sd*`, fork-bomb, etc. set.
  - `_validate_driver_text` requires ENVIRONMENT, PRIMARY TOOLS, PATTERNS, **PITFALLS**, RESPONSE FORMAT, COMPLETION — each exactly once, in canonical order, with fenced code blocks stripped before scanning. `generate_driver` refuses to call the LLM on an empty `ExplorationResult` (no successful probes AND no DONE summary).
  - Pane teardown kills the tmux session and `shutil.rmtree`s the per-tool `/tmp/clive/explore-<tool>-<hex>/` — exploration panes are one-shot, never reused.
- `drivers/` — per-app-type prompt cards (auto-discovered; `explore.md` is the exploration driver used by `discovery/`)
- `tools/` — helper shell scripts (`claude.sh`, `youtube.sh`, `podcast.sh`, ...)

Key dataclasses live in `src/clive/models.py` (`Subtask`, `Plan`, `SubtaskResult`, `PaneInfo`).

## Remote / agent-to-agent

SSH is the inter-habitat protocol. `clive@host` addressing (`networking/agents.py`) auto-resolves a host, opens an SSH pane with `ControlMaster=auto` (sockets under `~/.clive/ssh/`), and routes tasks. The remote runs in conversational mode emitting **framed, nonce-authenticated** turn-state messages (`networking/protocol.py`). Never bypass the frame authentication — it's what stops a compromised remote LLM from forging completion.

**BYOLLM**: cloud provider keys forward via SSH `SendEnv`; local providers (LMStudio/Ollama) switch the remote to `LLM_PROVIDER=delegate` and round-trip every inference back over the SSH channel via `llm/delegate_client.py`. See `docs/byollm-delegate.md`.

Named instances (`--name foo`) are long-running, addressable processes; registry at `~/.clive/instances/`. Local-first resolution — `clive@foo` checks the local registry before SSH.

## Self-modification (experimental, off by default)

`selfmod/` implements Proposer → Reviewer → Auditor → deterministic Gate. Gate is regex-only (cannot be talked past). File tiers (IMMUTABLE / GOVERNANCE / CORE / STANDARD / OPEN) determine how many approvals a change needs. Enable via `CLIVE_EXPERIMENTAL_SELFMOD=1`. `gate.py` and `.clive/constitution.md` are IMMUTABLE.

## Conventions & gotchas

- **Textual caveat**: `Screen.task` is reserved — use `task_text` instead (bit us already).
- **Completion signal is `DONE:`**, not XML. Don't reintroduce XML wrappers.
- **Pane-as-conversation**: the pane's scrollback *is* the sub-agent's context. The screen content carries command history + output; prompts do not duplicate it.
- **Shared working dir** is `/tmp/clive/` (per-session subdirs). Use this as inter-subtask file channel.
- **Telemetry vs result**: `output.py` routes telemetry to stderr and results to stdout — preserve this so `-q` works as a shell primitive.
- Tests run against the package via `sys.path` injection in `tests/conftest.py`. Don't convert imports to dotted form without updating that shim and `clive.py`.
- **Streaming observation:** Phase 1 is default-on as of 2026-04-16. `CLIVE_STREAMING_OBS=0` opts out. FIFO + byte-classifier pipeline; see `docs/plans/2026-04-16-streaming-observation-design.md`. **Phase 2 speculation** (version-stamped LLM calls cancel-on-supersede) is behind `CLIVE_SPECULATE=1` (default off). Scheduler counters logged at runner teardown.

## Reference docs in-repo

- `SPEC.md`, `docs/SPEC.md`, `docs/SPEC-v3.md` — architecture spec evolution
- `docs/plans/` — planning docs (see `2026-04-09-pane-core-refocus.md` for the XML-strip rationale)
- `TOOLS.md` — tool catalog and how to create custom profiles
- `docs/byollm-delegate.md` — remote LLM delegation protocol & threat model
