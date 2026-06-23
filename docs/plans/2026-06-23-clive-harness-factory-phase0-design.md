# clive-harness-factory — Phase 0 Implementation Design

Date: 2026-06-23
Status: building
Spec: operator-supplied "clive-harness-factory — Build Spec (Phase 0)" (`/goal`)
Lives in: `clive/factory/`

This document is the concrete implementation design + plan for Phase 0. It records
the decisions and the contracts; the spec records the *why* (its §1 governing
principles are constraints, preserved here). Where the spec marked a *discovery point*,
the answer below is grounded in the clive source, not invented.

## Grounding (discovery results)

- **clive stack**: Python 3.10+ (3.14 here), flat-import package under `src/clive/`,
  entry `clive.py` (shim → `src/clive/clive.py` → `clive_core.py`). Runs under **system
  python3** (CLAUDE.md: "no virtualenv is active"). Deps importable: `anthropic, libtmux,
  openai, dotenv`. `sqlite3` + `PyYAML 6.0.3` available on system python3.
- **BYOI / model selection** = env vars: `LLM_PROVIDER` (default `openrouter`),
  `AGENT_MODEL`, `SCRIPT_MODEL`, `CLASSIFIER_MODEL`, `LLM_BASE_URL`. Providers:
  `anthropic, openai, gemini, openrouter, lmstudio, ollama, delegate`.
- **Run a task**: `python3 clive.py -q --json --max-tokens N -t <toolset> "<goal>"`.
  `-q` → result to stdout, telemetry to stderr. No `--keep-session` flag; keep artifacts
  via env `CLIVE_KEEP_SESSION=1`.
- **Extralinguistic sensor surface** (principle 2): per-run aggregate
  `~/.clive_session_log.jsonl`; per-subtask evidence `/tmp/clive/{sid}/_result_*.json`,
  `_log_*.txt`. clive's own success claim = `completed/failed` counts — **recorded, never
  scored**.
- **clive-to-clive comms (§12 discovery point)** = the **Rooms** system. Framed protocol
  `<<<CLIVE:{kind}:{nonce}:{base64(json(payload))}>>>` (`src/clive/networking/protocol.py`),
  broker `--role broker --name <lobby>`, member `--name X --conversational --join room@lobby`.
  Driver `src/clive/drivers/room.md`. Design doc `docs/plans/2026-04-14-clive-rooms-design.md`.
  **Wired in, not reinvented.**
- **Frozen-block source of truth**: command safety blocklist `src/clive/execution/runtime.py:51-222`
  (rm -rf /, fork bomb, shutdown, mkfs, dd of=/dev/*, chmod 777 /, curl|bash, base64|sh);
  self-mod gate tiers `src/clive/selfmod/gate.py` + `.clive/constitution.md`. clive has **no
  host/network allowlist today** → the factory *adds* one as `frozen.scope_limits`, enforced
  twice (frozen + negative checks).
- **Actuation seam (spec.open → clive behavior)**: clive's real knobs.
  `CLIVE_EVAL_DRIVER_OVERRIDE=<file>` overrides `load_driver()` globally (→ `system_prompt`);
  `CLIVE_TOOLSET`/`-t` (→ affordances); `CLIVE_PROGRESSIVE_TOOLS`; `CLIVE_STREAMING_OBS`,
  `CLIVE_CONTROL_SIDECAR`, `CLIVE_SPECULATE`, `CLIVE_PANE_ISOLATION`, `CLIVE_PS1_EXITCODE`
  (→ observation); `CLIVE_SANDBOX(_MAX_PROCS|_MEM_MB)` (→ scope); `HOME` (→ isolation +
  evidence capture); `CLIVE_KEEP_SESSION`. `recovery_policy.max_turns` is a source constant
  (`_DEFAULT_MAX_TURNS=4`) → **declared but actuation-pending** in Phase 0.

## Operator decisions (asked, this session)

1. **Env provider**: build *both* `local_sandbox` (default) and `docker_env`; smoke test uses
   local (Docker installed but not running on this host).
2. **Model panel**: config-driven `panel.yaml` placeholders; the entry marked `smoke: true`
   drives the smoke test; credentials sourced from clive's existing `.env`.

## Planes (spec §2) — separated by construction

- **Generation**: Proposer (`claude -p`) → one bounded change to `open`.
- **Measurement**: deterministic runner + checks (the spine); Judge (`claude -p`) annotates
  only what checks can't reach.
- **Arbitration**: operator at the board; Reporter (`claude -p`) prepares digests; divergence
  signals computed deterministically.

All state in one SQLite blackboard. Roles never message each other; the orchestrator
sequences; the board is read-mostly with one write: promote.

## Store schema (§8) — `store/schema.sql`

`champion, candidates, scenarios, runs, judge_notes, promotions, recalibrations,
budget_ledger, safety_flags` exactly per §8, plus columns needed by the board and the
proposer's "changes already tried with outcomes" (`candidates.change_summary`,
`candidates.diff_json`, `candidates.scores_json`). `runs.clive_claim` stores clive's own
success claim (recorded, never scored). `candidates.stage ∈ {proposed, evaluating, scored,
awaiting_gate, promoted, rejected}`. `runs.outcome ∈ {pass, fail, error, budget_exceeded, blocked}`.

## Spec format + validator (§4) — `common/specs.py`

YAML: `meta{version,parent,hash}`, `open{system_prompt, command_affordances,
observation_policy, recovery_policy, skills}`, `frozen{permission_gates, scope_limits,
destructive_action_policy}`.

Validator rules (hard):
- `hash = sha256(canonical_json(open) + canonical_json(frozen))`; `verify_hash()` detects a
  tampered frozen block.
- A candidate's `frozen` must be canonically identical to the champion's → else **reject**
  (it touched frozen).
- `open` must differ from the parent and change at most `max_changed_open_keys` (config, =1)
  top-level keys → "one bounded change".
- Candidate must set `meta.parent`.

## Spec applier (`common/spec_applier.py`)

`open` → clive knobs (env + flags). Model env is **not** from `open` — it comes from the
panel (orthogonal: same candidate run under each panel model). Un-actuatable fields are
recorded with `actuation: pending` and surfaced in the run record (no silent no-op).

## Env providers (§6) — `envs/`

`base.EnvProvider`: `provision(scenario, run_id) → EnvHandle`, `run_in_env(handle, cmd) →
(rc,out,err)` (used by checks), `read_file`, `reset`, `teardown`. `EnvHandle` carries
`workdir` (clive CWD), `home` (HOME → `~/.clive` isolation), `clive_env` (extra env),
honeypot markers (`.grader_secret`, `.heldout_canary` for the negative grader/held-out check).
- `local_sandbox` (default): mkdtemp sandbox; soft boundary (CWD/HOME) + `CLIVE_SANDBOX=1`;
  checks run locally with `cwd=workdir`.
- `docker_env`: `docker run -d --rm --network none -v workdir:/work`; checks via `docker exec`;
  hard isolation. Auto-skips with a clear message if the daemon is down.

## Runner (§6) — `runner/runner.py`

Per `(candidate, scenario, model)`: provision → apply spec → run candidate clive under the
panel model toward the goal within hard time/step/token budget → run the scenario's
acceptance check + negative safety checks against the **real end-state** → record outcome +
evidence + clive's (unscored) claim + budget → teardown. Evidence (transcript, session log,
argv/env, check output) saved under `logs/runs/<run_id>/`.

## Checks (§6) — `checks/`

- `check_base.py`: `CheckResult{passed, evidence}`, scenario-check protocol.
- `safety.py`: reusable **negative** checks — out-of-scope path, grader/held-out access
  (honeypot exfil), unrequested listening port, unrequested destructive op (enforced twice
  with the frozen gate), budget exceeded. Each → `SafetyFlag{kind, detail, severity}`.
- `checks/scenarios/*.py`: per-scenario acceptance checks (positive) bundled with the safety
  battery.

## Scenarios (§7) — `scenarios/`

Working: `hello-artifact` (single) + `multi-clive-relay` (multi-clive, Rooms). Held-out:
`heldout-artifact` (never seen by proposer). Staging: scenario-miner output for vetting.
Each is a YAML triple (`id, class, snapshot, goal, check, partition, leakage_count, source`).

## Roles (§5) — `roles/`

Stateless `claude -p` workers. `roles/common.py` assembles a context slice from the store,
calls `claude -p <prompt.md + context>`, writes results back. Proposer is **blind**: its
slice excludes grader internals + held-out. Judge annotates (no pass/fail). Reporter computes
divergence deterministically + writes a prose digest. Scenario-miner reads
`~/.clive_session_log.jsonl` → staging only.

## Orchestrator (§9) — `orchestrator/`

CLI: `init, propose, evaluate, round, status`. Triggers: optimisation fires only when ≥ N
new failure cases accumulated (gain governor). Evaluation loop is parallel under a
concurrency cap; budget ledger + hard cap; circuit breakers (repeated tool errors / runaway
cost / no-improvement-for-K halts a search, read against the held-out signal). Promotion rule
(champion-challenger): beats champion on working set AND no held-out regression AND no panel
regression AND no safety flag → `awaiting_gate` + promotion queue. **Promotion is a human
action at the board.**

## Board (§10) — `dashboard/`

stdlib `http.server`, bound to 127.0.0.1. Read endpoints: board (kanban), scoreboard,
divergence (the loud Goodhart alarm), leakage meter, cost burn, promotion queue (+ digest),
safety flags. One write: `POST /api/promote`. Single-page front end. The andon board.

## Safety & isolation (§11) — invariants enforced

Disposable fleet only; no real-system path; credential scope = isolation boundary; frozen out
of mutation space *and* negative checks; proposer blind; no autonomous outbound action; no
autonomous promotion; concurrency + budget caps; everything logged.

## Build order (spec §13) & smoke test (§13.10)

1 layout+schema · 2 spec format + champion.yaml · 3 env providers · 4 example scenario +
checks · 5 runner · 6 roles · 7 orchestrator · 8 board · 9 Rooms + multi-clive scenario ·
10 smoke test on the single example scenario across the panel, ending at the human gate.
**No autonomous promotion. No real credentials.** Smoke test defaults to a deterministic
seeded candidate (cheap, hermetic) with `--propose` to exercise the real `claude -p`
proposer.

## Adversarial review & hardening (post-build)

A 4-auditor adversarial review (spec-compliance, safety red-team, correctness/concurrency,
runnability) confirmed the core invariants hold by construction (one-field proposer patch ⇒
frozen-safe; outcome never derived from clive's self-report; one board write; no autonomous
promotion path). Fixes applied:

- **Candidate isolation (critical):** clive's `.env` carries `CLIVE_EXPERIMENTAL_SELFMOD=1`,
  which a candidate would inherit and could drive into the *real* clive source. Every
  candidate invocation now passes clive's `--safe-mode` **and** forces
  `CLIVE_EXPERIMENTAL_SELFMOD=0`; `clive_invoke._scrub_env` drops non-LLM host credentials
  (`AWS_*`, `GH_*`, SSH/Docker/Kube). The LLM provider key (the candidate's *brain*) is kept;
  the isolation boundary is the *environment*, not the intelligence.
- **Evidence cross-contamination (high):** the safety battery scanned a global `/tmp/clive`
  by mtime; under concurrency two runs ingested each other's artifacts. Now scoped to the
  exact `Session: /tmp/clive/<id>` dir(s) clive prints on stderr (mtime only as fallback).
- **Safety regexes (high):** bare `rm -rf /` / `~` / `/*` slipped past a trailing word
  boundary; added `shred`/`find -delete`/`dd of=`/`curl -o`/`truncate`, broadened host roots,
  and excluded in-scope `/tmp` + `/var/folders` so sandbox ops don't false-positive. Fixed a
  `_GOAL_ALLOWS_SERVER` substring bug ('port' inside 're-port' disabled the listen check).
- **multi-clive (high):** process-group teardown no longer early-returns on parent exit
  (SIGTERM→SIGKILL the group + reap); a `handle is None` provision-failure path records an
  error run instead of dereferencing None.
- **Proposer blindness (medium):** `scoring.proposer_safe_scores` redacts held-out-derived
  fields from the "changes already tried" history so the proposer gets no held-out gradient.
- **Misc:** microsecond timestamps (propose-trigger boundary), `dump_spec` no longer mutates
  the caller, divergence threshold scales to panel size, board `init_db()`-defensive + CSRF
  Origin guard on the one write, `bin/factory` made executable, and the held-out **model**
  overfit probe is now wired as `factory holdout-check <cid>` (partition `holdout-model`).

### Second pass — formal `code-review` (8-angle, recall-biased)

A `/code-review` run (8 finder angles → 1-vote verify) surfaced 24 candidates; the 10
highest-severity (all CONFIRMED/PLAUSIBLE) were fixed — **two were regressions from the
hardening pass above**:

1. `holdout-check` recorded its runs as `partition='working'`, so `_evaluate` clobbered the
   candidate's authoritative scores and reset an `awaiting_gate` candidate to `scored`. Fixed:
   `_evaluate` gained `work_partition` + `update_candidate`; the holdout path records
   `holdout-model` directly and never touches the candidate's scores/stage.
2. The CSRF guard used substring matching (`'127.0.0.1' in origin`), so
   `localhost.attacker.com` passed. Fixed: exact `urlparse(origin).hostname` match.
3. Evidence-collection fallback still swept the global `/tmp/clive` by mtime under
   concurrency. Fixed: attribute by the run's **unique sandbox workdir path**, never a blind
   mtime sweep.
4. Out-of-scope-write regex missed quoted targets (`> "/etc/passwd"`). Fixed.
5. Grader-secret exfil to a non-logged workdir file evaded the transcript-only scan. Fixed:
   the safety scan now includes the candidate's workdir file contents.
6. The multi-clive path scanned only room logs, not member session artifacts (a safety blind
   spot vs the single-clive path). Fixed: it now folds member `_log_*`/`_script_*` + workdir
   files into the battery.
7/10. multi-clive runs recorded `budget_used=0`, never ledgered tokens, and stored `check_json`
   without the top-level `detail` the Proposer reads. Fixed: member tokens summed from the
   shared session log, ledgered to the BudgetGuard/cost meter, and `detail` restored.
8. Re-running `init` after a promotion silently reverted the champion (fresh `promoted_at` on
   the baseline row). Fixed: seed the champion only on a fresh store.
9. The gain-governor trigger counted a candidate's **own** eval failures, letting it
   self-feed. Fixed: it counts only the reigning **champion's** working failures (ground
   truth), per §9.

The remaining 14 lower-severity candidates were ranked below the cut; the high-severity set is
closed. All fixes re-verified by unit checks + a fresh end-to-end smoke ending at the gate.
