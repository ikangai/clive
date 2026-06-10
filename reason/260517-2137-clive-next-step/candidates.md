# Final round candidates (round 3) — for manual review

## Candidate A (winner, 3-2)

# Make `ScreenClassifier` command-aware in one PR, gated on a 30% escalation-rate drop on the existing scenario bench — no protocol seam, no decision log, no v2.

**Thesis:** The incumbent ships a seam, a shadow, a redacted JSONL log, a replay tool, and a promotion rule before proving a single command-aware rule beats Legacy on the bench Clive *already has*. That is four artifacts in service of one missing experiment. The real next step is the experiment: thread `last_command` into `ScreenClassifier.classify()` and add an `INFORMATIONAL_NONZERO` table (`git diff --quiet`, `grep`, `test`, `[`, `cmp -s`, `diff -q`), then measure on `evals/observation/scenarios.py` against `metrics.py:RunResult.cost_tokens`. If it wins, the seam writes itself later from two known-good strategies. If it doesn't, the seam was scaffolding for nothing.

## Grounded locus

- `observation/observation.py:56` — `classify(self, screen, exit_code=None)` has no `last_command` parameter. Branch 3 (`observation.py:92-100`) unconditionally sets `needs_llm=True` for every non-zero exit. That single branch is the dominant escalation surface for shell panes.
- `interactive_runner.py:352` and `toolcall_runner.py:246` already have the command string inline (it was just typed into the pane via `_send_command` / the `run_command` tool call). Passing it to `classify()` is a one-line change at each site; nothing in `models.py` or the dataclass needs to move.
- `evals/observation/scenarios.py`, `metrics.py:RunResult`, and `phase1-report.md` already exist. The harness already aggregates `cost_tokens` and `missed_rate` per (scenario, mode). The "mode" axis is exactly where a `phase1+cmdaware` row drops in.

## First PR — exact contents

1. `observation/observation.py`: add `last_command: str | None = None` to `classify()`; add module-level `INFORMATIONAL_NONZERO = {"git diff --quiet", "git diff --exit-code", "grep", "egrep", "fgrep", "test", "[", "cmp -s", "diff -q", "pgrep"}` matched against `shlex.split(last_command)[0]` plus a tiny suffix check for `git diff --quiet`. When matched and exit_code != 0, return `EventType.SUCCESS` with `needs_llm=False` and `summary=f"informational non-zero (exit {exit_code})"`. ~25 lines.
2. `interactive_runner.py:352` and `toolcall_runner.py:246`: pass `last_command=cmd` (already in scope as the just-sent command). 2 lines.
3. `tests/test_observation.py`: 6 cases — `git diff --quiet` exit 1 → SUCCESS, `grep foo nofile` exit 1 → SUCCESS, `ls /nonexistent` exit 2 → ERROR (unchanged), and the three with `last_command=None` to prove default behaviour is byte-identical to today.
4. `evals/observation/scenarios.py`: add 4 scenarios — `cmd_aware_git_diff_clean`, `cmd_aware_grep_miss`, `cmd_aware_test_false`, plus one negative (`make` exit 2) that must *still* escalate. Run `latency_bench.py` and append a `cmdaware` column to `phase1-report.md`.

That is three production files and two test/eval files. No new module. No protocol. No log. No environment variable.

## Crisp success metric

On the post-PR bench, over the union of existing phase1 scenarios plus the four new ones, median `cost_tokens` for shell-mode runs drops ≥30% **and** `missed_rate` does not increase versus the current `phase1` row in `phase1-report.json`. Both numbers are already produced by `aggregate()` in `evals/observation/metrics.py:34`. PR merges iff the diff in the report shows both inequalities. No human judgement, no opt-in corpus, no redaction policy required.

## Why this beats the incumbent's move

The incumbent's seam is correct *eventually* but premature: a `ClassifierStrategy` Protocol with one real implementation and one discard-only shadow is a refactor in search of a second strategy. The 30%-drop experiment **produces that second strategy**, on the existing bench, with falsifiable output. If `INFORMATIONAL_NONZERO` wins, the seam in round 4 has two concrete shapes to abstract over (Legacy and CmdAware), and the Protocol is obvious. If it loses, we learned the dominant escalation surface isn't command-shape but error-text-shape, and the incumbent's seam would have been pointed the wrong way.

## Tradeoff accepted

We forgo the architectural reusability the seam would advertise, and we tie ourselves to a hand-curated command list (which the autoresearch driver-findings note is exactly the regime where +37pp wins came from — small, curated, measured). We also defer the privacy-sensitive decision log entirely; opt-in real-traffic logging is a separable feature whose design should be driven by a concrete classifier deficit the bench couldn't surface, not pre-built in case one appears.
---

## Candidate B

# Land a structured turn-trace ledger in `execution/runtime.py` — every runner writes one JSONL record per (command, exit, classifier verdict, downstream-turn outcome) — as the substrate for every later observation/driver/mode-pick decision.

**Thesis:** Clive's architectural bottleneck is not "the classifier escalates too often"; it is that **no decision in the system is currently judged against what actually happened on a user's machine.** Every knob — classifier rules, driver RESPONSE FORMAT, mode picker, repair retries, speculation — is tuned against hand-written scenarios authored by the same person tuning the knob. The next concrete architectural step is the smallest thing that breaks that loop: a content-addressed turn-trace ledger emitted by all runners, with a stable schema, on by default, locally stored, opt-out per pane. Not telemetry. Not analytics. A first-class signal channel whose absence is the reason the eval bench has become an echo chamber.

## Locus

- **`src/clive/observation/trace.py`** (new, ~120 lines) — defines `TurnTrace` dataclass: `session_id`, `subtask_id`, `pane_id`, `mode`, `turn_idx`, `command` (or `command_hash` + length when redaction on), `exit_code`, `screen_bytes_hash`, `classifier_verdict` (event_type, needs_llm, summary), `decided_action` (one of: `accept`, `escalate_to_llm`, `repair_retry`, `complete`, `abort`), `downstream_exit_after_action`, `tokens_in`, `tokens_out`, `wall_ms`, `model_tier`. Plus a `TraceWriter` with line-buffered append to `~/.clive/traces/{YYYY-MM-DD}/{session}.jsonl`.
- **`src/clive/execution/runtime.py`** — single chokepoint every runner already crosses for command dispatch + observation. Add `RuntimeContext.emit_trace(trace: TurnTrace)`. Each runner (`script_runner.py`, `planned_runner.py`, `interactive_runner.py`, `toolcall_runner.py`) gets a 2-line call site after the classifier verdict + after the *next* turn's exit is known, so we capture the **pair** (verdict, downstream truth) — that pair is what the existing bench cannot generate.
- **`src/clive/session/session.py`** — register a writer per session in setup so the path is owned, flushed on shutdown, and rotates daily. ~10 lines.
- **`evals/observation/replay.py`** (new, ~60 lines) — read traces, regenerate `phase1-report.json`-shaped aggregates over real runs, not hand-written scenarios. This is the piece that closes the loop: the next classifier change is judged against the **previous month's traces**, not new scenarios the same author writes.

## Rationale tied to current state

Four signals from the brief and the codebase converge here:

1. The observation loop already produces a structured classifier verdict (`observation.py` returns an `EventType` + `needs_llm` + `summary`) — but it is consumed and discarded inside the runner's local scope. The single highest-leverage thing we own is currently thrown away every turn.
2. The brief calls out that **autoresearch's biggest win (+37pp) came from a measured driver experiment**. Driver experiments need a corpus of real turns to score against; we have none. Every future driver tweak will be argued from scenarios, not behavior.
3. The 3-tier router, the per-pane model tiers, and the speculative phase 2 all make implicit cost/quality tradeoffs whose calibration **requires longitudinal pair data** — verdict-vs-outcome — that no current path emits.
4. The framed nonce-authenticated remote protocol already serializes turn state for `clive@host`; the in-process trace is the same shape one level up. A unified schema means remote traces stream home for free in round 4.

This is the substrate move that makes the next five architectural decisions falsifiable instead of rhetorical.

## Tradeoffs accepted

- **Disk + privacy surface.** Local-only by default, env opt-out (`CLIVE_TRACE=0`), command field hashed when `CLIVE_TRACE_REDACT=1`, and a documented schema-versioning rule (`v=1`). No upload path in this PR — that is the next architectural decision and now has data to argue with.
- **Schema lock-in.** Once writers exist, the schema is load-bearing; the cost is borne now by versioning every record and reserving a `meta: dict` slot. Cheaper than the alternative (no signal at all).
- **One more thing every runner must remember to call.** Mitigated by routing through `runtime.py` — the one module every mode already depends on — and a runner-base helper so the omission becomes a test failure.
- **Not a user-visible win this PR.** Correctly so: this is the missing instrument, not the experiment. The next PR (classifier change, driver tweak, mode-pick heuristic — any of them) will run against real traces and produce an *honest* number. That is the architectural unlock; the headline metric is the next PR's, not this one's.

What this explicitly is not: a logging library, an analytics pipeline, a UI, or a new abstraction layer between runners and the classifier. It is one dataclass, one writer, one replay reader, and four 2-line call sites — sized exactly to the gap the current bench cannot fill.

---

## Candidate AB (runner-up)

[A is right that the next move must be a falsifiable experiment, not a refactor in search of a second strategy. B is right that the bench is an echo chamber — but proposes a substrate PR with no headline win, which is exactly the "always-on observability" the brief forbids as an architectural step. The synthesis: ship A's command-aware classifier change AND emit a minimal verdict/outcome trace from the same chokepoint, in one PR, judged on A's 30%-drop metric. The trace is justified because it is the only honest way to validate A's win once the bench saturates.]

**Thesis:** In one PR, thread `last_command` into `ScreenClassifier.classify()` with an `INFORMATIONAL_NONZERO` table, and at the same `runtime.py` chokepoint emit a versioned `TurnTrace` JSONL record pairing (classifier verdict, downstream exit). Merge iff median `cost_tokens` drops ≥30% and `missed_rate` does not rise on the existing phase1 bench plus four new cmd-aware scenarios.

## Locus (three production files, two eval files, one new module)

- `observation/observation.py:56` — add `last_command: str | None = None` to `classify()`. Add module-level `INFORMATIONAL_NONZERO = {"git diff --quiet", "git diff --exit-code", "grep", "egrep", "fgrep", "test", "[", "cmp -s", "diff -q", "pgrep"}` matched against `shlex.split(last_command)[0]` with a suffix check for `git diff --quiet`. When matched and `exit_code != 0`, return `SUCCESS` with `needs_llm=False`, `summary=f"informational non-zero (exit {exit_code})"`. ~25 lines. Default `None` preserves byte-identical behaviour.
- `interactive_runner.py:352` and `toolcall_runner.py:246` — pass `last_command=cmd`; the string is already in scope. 2 lines each.
- `execution/runtime.py` — `RuntimeContext.emit_trace(TurnTrace)`. Called once after the classifier verdict and once after the *next* turn's exit is known, so the record carries the pair (verdict, downstream truth). This is the single chokepoint every runner already crosses; no per-runner glue beyond two call sites.
- `observation/trace.py` (new, ~80 lines) — `TurnTrace` dataclass (`session_id`, `subtask_id`, `pane_id`, `mode`, `turn_idx`, `command_hash`, `exit_code`, `classifier_verdict`, `decided_action`, `downstream_exit_after_action`, `tokens_in/out`, `wall_ms`, `model_tier`, `v=1`, `meta: dict`) plus a line-buffered `TraceWriter` to `~/.clive/traces/{YYYY-MM-DD}/{session}.jsonl`. Default-on, `CLIVE_TRACE=0` opts out, `CLIVE_TRACE_REDACT=1` hashes the command field.
- `evals/observation/scenarios.py` — four new scenarios (`cmd_aware_git_diff_clean`, `cmd_aware_grep_miss`, `cmd_aware_test_false`, plus `make` exit 2 that must *still* escalate). `phase1-report.md` gains a `cmdaware` column.
- `tests/test_observation.py` — 6 cases including three with `last_command=None` proving default behaviour is unchanged.

## Why this beats either pure move

A alone is a one-shot win whose generalisation depends on a hand-curated command list; the next driver tweak or mode-picker change will again be argued from scenarios the same author wrote. B alone is a substrate PR that the brief explicitly disqualifies as an architectural step because it carries no falsifiable headline. Combined, the trace is no longer pure instrumentation — it is the validation channel for A's claim once the curated bench saturates, and it costs ~80 lines plus two call sites at the chokepoint A is already touching. Both pieces share the same locus (`runtime.py` plus the classifier callsite), so the marginal cost of adding the ledger is small enough that the PR can still be merged on A's metric alone.

A's argument that a `ClassifierStrategy` Protocol is premature stands; we are not introducing one. B's argument that a JSONL log is the architectural unlock stands only when grounded to a concrete experiment, which A supplies. The contradiction resolves: ship the experiment, capture the pair data the experiment will be re-validated against next quarter, do not ship the seam.

## Success metric

On the post-PR bench (existing phase1 scenarios ∪ four new cmd-aware scenarios), median `cost_tokens` for shell-mode runs drops ≥30% **and** `missed_rate` does not increase vs the current `phase1` row in `phase1-report.json`. Both numbers already come out of `aggregate()` in `evals/observation/metrics.py:34`. PR merges iff the diff in the report shows both inequalities. The trace ledger is not gated on a metric — it is the substrate that makes the *next* PR's metric honest.

## Tradeoffs accepted

We tie ourselves to a hand-curated command list (the regime where autoresearch's +37pp came from — small, curated, measured). We accept a disk-and-privacy surface mitigated by local-only storage, opt-out env var, optional command hashing, and a `v=1` schema field with a reserved `meta` slot. We forgo a `ClassifierStrategy` Protocol and any decision-log upload path; both become trivially designable in round 4 from two known-good classifier shapes and a month of real traces, rather than rhetorically now.
