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

Three phases by cost — WAIT (free: markers, polling, exit codes) → OBSERVE (cheap: `observation/observation.py` regex `ScreenClassifier` → compact events like `[OK exit:0] ...`) → DECIDE (expensive: main model, only when classifier escalates). This cuts tokens 60-80%. `observation/context_compress.py` runs a cheap model to progressively compress old turns rather than dropping them.

### Per-pane models

Each pane declares a model tier (`fast`/`default`) via its driver frontmatter. Shell/data use Haiku/Flash; browser/email use the default. The tier label resolves to a concrete model via the active provider (`session/session.py`, `llm/llm.py`).

### Command protocol (non-tool-calling path)

Plain text. LLM emits ```bash fenced blocks; `observation/command_extract.py` parses them. Completion signal is literally `DONE: <summary>` — **not XML**. This was an explicit simplification ("Pane Core Refocus", 2026-04-09) — do not reintroduce XML or a PaneAgent/SharedBrain abstraction.

### Drivers

`drivers/*.md` are auto-discovered per `app_type` (shell, browser, email, ...). Compact reference-card format with frontmatter for model tier. The driver prompt is the persona the LLM adopts inside that pane. The "RESPONSE FORMAT" section of a driver is the highest-leverage part — see `memory/project_autoresearch_driver_findings.md`.

### Toolsets

`session/toolsets.py` — profiles (`minimal`, `standard`, `full`, `remote`, ...) composed of categories via `+` syntax (e.g. `-t standard+media+ai`). `resolve_toolset()` returns panes, commands, endpoints. Toolsets are **dynamic**: categories expand at runtime via `_expand_toolset()`. `commands.py:check_commands()` verifies installed CLIs at startup.

### Router (3-tier intent classification)

`src/clive/router.py` routes user input through: (1) direct match → (2) cheap classifier → (3) full planner. Most tasks resolve at tier 1 or 2.

## Source layout (src/clive/)

Flat imports work because `conftest.py` and `clive.py` put `src/clive/` on `sys.path`. Subpackages are organizational, not import-path scoping: inside `src/clive/` modules import each other flat (`from models import Subtask`), not via dotted paths.

- `llm/` — provider-agnostic client, prompts, native tool defs, delegate client
- `planning/` — planner, DAG scheduler, summarizer
- `execution/` — mode-specific runners (script, planned, interactive, toolcall, skill) + shared `runtime.py`
- `observation/` — screen classifier, diff, compression, completion detection, command extraction
- `session/` — tmux session/pane management, toolsets, command checks
- `networking/` — `clive@host` addressing, framed protocol, instance registry, dashboard, SSH/SCP
- `tui/` — Textual TUI + slash commands
- `selfmod/` — experimental self-modification (governed by `.clive/constitution.md`, guarded by `gate.py` regex)
- `drivers/` — per-app-type prompt cards
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

## Reference docs in-repo

- `SPEC.md`, `docs/SPEC.md`, `docs/SPEC-v3.md` — architecture spec evolution
- `docs/plans/` — planning docs (see `2026-04-09-pane-core-refocus.md` for the XML-strip rationale)
- `TOOLS.md` — tool catalog and how to create custom profiles
- `docs/byollm-delegate.md` — remote LLM delegation protocol & threat model
