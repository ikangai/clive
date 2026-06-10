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