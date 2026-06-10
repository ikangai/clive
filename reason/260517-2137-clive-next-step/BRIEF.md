# Clive — Current State Brief (for adversarial reasoning loop)

## Identity
Clive is an **environment-interface agent**: an LLM drives CLI tools through tmux by reading
the terminal screen and typing keystrokes. No MCP, no tool schemas, no REST. The terminal *is*
the interface. Any LLM provider can orchestrate; Claude is just one tool in the toolset.

Governing principle: **LLM where judgment is required, shell everywhere else.**

## Pipeline
Plan → Execute (parallel DAG over tmux panes, per-subtask isolated `/tmp/clive/{session}/`) → Summarize.

## Execution modes (planner picks cheapest)
- `direct` — literal command, 0 LLM calls
- `script` — single-step deterministic, 1 LLM call + repair retries
- `planned` — multi-step deterministic, 1 LLM call up-front
- `interactive` — read-think-type loop, N turns
- `streaming` — interactive with mid-output intervention
- tool-calling — native run_command/read_screen/complete tools, N turns

## Cost-optimization stack
- 3-tier router: direct match → cheap classifier → full planner (most tasks resolve tier 1-2)
- Per-pane model tiers: shell/data on Haiku/Flash, browser/email on default model
- Observation loop: WAIT (free polling/markers) → OBSERVE (regex ScreenClassifier emits compact `[OK exit:0]` events) → DECIDE (main model only when classifier escalates). Cuts tokens 60-80%.
- Progressive context compression: cheap model compresses old turns rather than dropping them

## Command protocol (non-tool-calling path)
Plain text. LLM emits ```bash fenced blocks; `command_extract.py` parses them.
Completion signal is literally `DONE: <summary>`. **Not XML.**
Stripped in the 2026-04-09 "Pane Core Refocus" — do not re-propose XML wrappers or PaneAgent/SharedBrain.

## Drivers
`drivers/*.md` per `app_type` (shell, browser, email, ...). Compact reference cards with frontmatter
for model tier. The "RESPONSE FORMAT" section is the highest-leverage part (autoresearch finding: +37pp on shell evals).

## Toolsets
`session/toolsets.py` — profiles (minimal/standard/full/remote) composed of categories via `+` syntax
(e.g. `-t standard+media+ai`). Dynamic expansion at runtime. `commands.py:check_commands()` verifies CLIs at startup.

## Remote / agent-to-agent
SSH is the inter-habitat protocol. `clive@host` addressing auto-resolves and opens an SSH pane with
`ControlMaster=auto`. Remote runs in conversational mode emitting **framed, nonce-authenticated**
turn-state messages. Never bypass frame auth — it's what stops a compromised remote LLM from forging completion.
**BYOLLM**: cloud keys forward via SSH `SendEnv`; local providers (LMStudio/Ollama) switch remote to
`LLM_PROVIDER=delegate` and round-trip every inference back over the channel.
Named instances (`--name foo`) are long-running, addressable processes; registry at `~/.clive/instances/`.

## Streaming observation
Phase 1 (FIFO + byte-classifier) default-on as of 2026-04-16. `CLIVE_STREAMING_OBS=0` opts out.
**Phase 2 speculation** (version-stamped LLM calls cancel-on-supersede) exists in `speculative.py` but is
behind `CLIVE_SPECULATE=1`, default off.

## Visible-but-experimental subsystems (in src/clive/ tree, not in default path)
- `selfmod/` — Proposer → Reviewer → Auditor → deterministic regex Gate. Enabled via `CLIVE_EXPERIMENTAL_SELFMOD=1`. `.clive/constitution.md` and `gate.py` IMMUTABLE.
- `evolution/` (`evolve.py`, `evolve_fitness.py`, `evolve_mutate.py`) — fitness-based self-evolution
- `lobby_server.py`, `lobby_client.py`, `lobby_state.py`, `lobby_connector.py`, `room_runner.py`, `room_participant.py` — multi-clive "rooms" for collaborative work (2026-04-14 design)
- `sandbox/` — isolated execution
- `skills_data/`, `skills.py`, `skill_runner.py` — Anthropic-style skill packs
- `speculative.py` — Phase 2 streaming speculation
- `agents_doctor.py` — health checks for agent-to-agent links

## Recent plans (chronological, last 6 weeks)
- 2026-04-08 — loopback-agent-test, script-mode-speedup, tiered-intent-classification
- 2026-04-09 — driver-quality-evals, instance-dashboard-design, **pane-core-refocus (XML strip)**, phase3-production-hardening, tool-configuration
- 2026-04-10 — remote-clive-byollm-delegation
- 2026-04-13 — architecture-improvements
- 2026-04-14 — clive-rooms-design, observation-loop-efficiency, repo-restructure
- 2026-04-16 — streaming-observation-design, streaming-observation

## Recent fixes (last ~10 days, on main)
PaneStream.unsubscribe wiring, relative imports for tui_* siblings, stale docstring in toolsets.py,
unbreaking bare `pytest` invocation, 0.7.1 release notes.

## What "next architectural step" means here
Identify ONE concrete next architectural move for Clive — name the change, the file/subsystem locus,
the rationale tied to current state, and the tradeoff being accepted. Be specific. Avoid generic
"add more tests" or "improve observability" — those are always-on, not architectural steps.
