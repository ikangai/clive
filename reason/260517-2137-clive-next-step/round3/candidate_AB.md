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
