# clive-harness-factory (Phase 0)

An automated system that **proposes, evaluates, and promotes** improvements to
**clive** — the bring-your-own-intelligence harness for driving remote systems
over a shell. This is **Phase 0**: the factory scaffolding and the operator's
board, ready to be seeded with scenarios and started by hand.

> Phase 0 does **not** enable autonomous promotion, and it wires **no credentials
> to any real system** and **no outbound real-world action**. A human (the
> **operator**) holds the promotion gate.

Design rationale and the grounding of every discovery point: see
[`../docs/plans/2026-06-23-clive-harness-factory-phase0-design.md`](../docs/plans/2026-06-23-clive-harness-factory-phase0-design.md).

## The idea in one breath

The **grader is the product**; the proposer is a thin wrapper. A candidate's score
comes from the **real end-state of a real shell**, read by deterministic checks —
never from clive's own report that it succeeded. Safety is **frozen** out of the
mutation space **and** scored as negative checks (enforced twice). The proposer is
**blind** to the grader internals and the held-out set. Gain is low by
construction: the optimisation loop fires only when reality supplies new failure
data, and concurrency is capped. The operator stands outside the loop.

## Three planes

- **Generation** — the **Proposer** (`claude -p`) emits one bounded change to the
  *open* part of the clive spec.
- **Measurement** — the deterministic **runner** provisions a disposable env, runs
  the candidate clive under each panel model against each scenario, and grades the
  resulting shell state with hidden checks. The **Judge** (`claude -p`) only
  annotates what the checks can't reach.
- **Arbitration** — the **operator** at the board, supported by the **Reporter**
  (`claude -p`) digest + deterministic divergence signals. Promotion happens here.

All state lives in one SQLite **blackboard** (`store/blackboard.db`). Roles never
message each other; they read/write the store, and the **orchestrator** sequences
them. The **board** is a read-mostly view with exactly one write: promotion.

## Layout

```
factory/
  orchestrator/   triggers, sequencing, concurrency + budget control
  roles/          proposer/ judge/ reporter/ scenario-miner/ (prompt.md + run.py) + common.py
  runner/         provision -> run candidate -> grade -> record -> teardown (+ multi_clive)
  envs/           local_sandbox (default) + docker_env + Dockerfile.base
  checks/         per-scenario acceptance checks + the negative safety battery
  scenarios/      working/ + held-out/ (kept separate on disk) + staging/ (mined)
  specs/          champion.yaml + candidates/<id>.yaml
  store/          schema.sql + migrations + blackboard.db
  dashboard/      server.py + static single-page board
  common/         config, store, specs+validator, spec_applier, scoring, budget, clive_invoke
  logs/runs/      transcripts + evidence per run
  bin/factory     CLI entrypoint
  smoke_test.py   end-to-end smoke test (§13.10)
```

## Install

```bash
pip install -r factory/requirements.txt --break-system-packages   # just pyyaml
# clive's own runtime (anthropic/openai/libtmux/dotenv) must be importable, and
# `tmux` + the `claude` CLI must be on PATH. Provider keys live in clive/.env.
```

## Quickstart

```bash
cd <clive repo root>
factory=factory/bin/factory          # or: python3 -m factory.orchestrator.orchestrator

$factory init           # apply schema; register champion + scenarios
$factory baseline       # evaluate the champion (working + held-out sample)
$factory status
$factory board          # open http://127.0.0.1:8787  (the andon board)

# end-to-end on the single example scenario, ending at the human gate:
$factory smoke --reset
```

### The loop, by hand

```bash
$factory propose             # fires ONLY if >= N new failures accrued (gain governor)
$factory evaluate <cid>      # candidate across working set x panel (concurrency-capped)
$factory round <cid>         # evaluate (+held-out sample) -> reporter -> gate to queue/rejected
$factory holdout-check <cid> # arbitration-cadence overfit probe under the held-out MODEL
$factory mine --limit 10     # scenario-miner -> staging/ for operator vetting
```

A candidate that clears the §9 rule (beats champion on the working set, no
held-out regression, no panel regression, no safety flag) lands in **awaiting
gate**. Promotion is then a **human action** on the board — the one write endpoint.

## How a candidate spec actuates clive (the bridge)

`common/spec_applier.py` renders a candidate's `open` block into clive's **real**
runtime knobs (env vars + flags) — no clive source edits:

| open field | clive knob |
|---|---|
| `system_prompt` | `CLIVE_EVAL_DRIVER_OVERRIDE=<file>` (global driver override) |
| `command_affordances.toolset` | `-t <spec>` + `CLIVE_TOOLSET` |
| `command_affordances.progressive_disclosure` | `CLIVE_PROGRESSIVE_TOOLS` |
| `observation_policy.{streaming,control_sidecar,speculate,pane_isolation,ps1_exitcode}` | the matching `CLIVE_*` env |
| `recovery_policy.max_turns` | source constant `_DEFAULT_MAX_TURNS` → recorded as *actuation pending* |

The panel model (provider/model) comes from `panel.yaml`, **not** from the spec —
the same candidate is run under each panel model.

## Disposable environments (§6, §11)

`config.yaml` `env.provider` selects `local` (default) or `docker`.
- **local_sandbox** — throwaway dir; HOME relocated into it (isolates `~/.clive` +
  evidence); `CLIVE_SANDBOX=1`. A *soft* boundary, honest about its limits.
- **docker_env** — `docker run --rm --network none`, mem/pid caps, bind-mounted
  workdir; checks run in-container. A *hard* boundary. Build the base image:
  `docker build -t clive-factory-env:base -f factory/envs/Dockerfile.base factory/envs`.

Either way, the negative safety battery (`checks/safety.py`) scans evidence for
out-of-scope paths, grader/held-out canary access, unrequested listening ports,
and destructive ops — encoding the frozen intent a second time.

**Candidate isolation (hardened).** Every candidate invocation passes clive's
`--safe-mode` and forces `CLIVE_EXPERIMENTAL_SELFMOD=0`, so a candidate can never
drive clive's self-modification pipeline into the *real* clive source. The
candidate process env is scrubbed of non-LLM host credentials (`AWS_*`, `GH_*`,
SSH/Docker/Kube, …) — the LLM provider key (the candidate's *intelligence*, not an
*environment* credential) is intentionally retained, since BYOI clive needs a
brain. The local sandbox is a **soft** boundary (cwd/HOME + `CLIVE_SANDBOX=1`);
use the **docker** provider (`--network none`) for a hard boundary when running
untrusted candidates.

## clive-to-clive comms (§12)

The existing **Rooms** system is integrated, not reinvented:
`runner/multi_clive.py` launches the real broker (`--role broker`) and member
clives (`--join room@lobby`), then grades the **world** result (the relayed token
on disk) and asserts the room transcript actually carried the message — because the
channel is a place where a claim can replace a fact. See scenario
`scenarios/working/multi-clive-relay.yaml`.

## Safety invariants (held by construction)

- Disposable fleet only; no path to any real system; credential scope is the
  isolation boundary.
- The spec validator **rejects** any candidate that mutates `frozen`, and the
  `meta.hash` over open+frozen makes tampering detectable.
- The Proposer is **blind**: its context slice excludes grader internals and
  held-out scenarios (assembled in `roles/common.py`).
- **No autonomous promotion** and **no autonomous outbound action**. The board's
  only write is a human promote.
- Concurrency + budget caps are enforced; circuit breakers halt on runaway
  cost/errors. Everything is logged under `logs/runs/`.

## Inspecting the store

```bash
sqlite3 factory/store/blackboard.db .tables
sqlite3 factory/store/blackboard.db "select id,stage,change_summary from candidates"
sqlite3 factory/store/blackboard.db .dump > snapshot.sql   # text snapshot to version-control
```
