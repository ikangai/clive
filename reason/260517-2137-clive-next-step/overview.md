# Reason loop — Clive's next architectural step

**Run:** 260517-2137 • **Mode:** creative (bounded, 3 iterations) • **Judges:** 5 (blind, randomized labels)
**Domain:** software architecture • **Chain:** predict

## Final winner

**Round 3 Candidate A** — "Make `ScreenClassifier` command-aware in one PR, gated on a 30%
escalation-rate drop on the existing scenario bench — no protocol seam, no decision log, no v2."

See: [round3/candidate_A.md](round3/candidate_A.md)

## The converged move (one sentence)

Thread `last_command` through `observation/observation.py:ScreenClassifier.classify()` and add an
`INFORMATIONAL_NONZERO` allowlist (`git diff --quiet`, `grep`, `test`, `[`, `cmp -s`, `diff -q`, `pgrep`)
so a non-zero exit from those commands stops short-circuiting to `needs_llm=True`. Validate on
`evals/observation/scenarios.py` + 4 new scenarios via `metrics.py:RunResult.cost_tokens`; merge iff
median shell-mode cost drops ≥30% **and** `missed_rate` does not increase versus today's `phase1` row.

## Lineage (round-by-round)

| Round | A words | B words | AB words | Winner | Vote tally (A/B/AB) | Incumbent after | Critic [F/M/Mi] |
|------:|--------:|--------:|---------:|:------:|:-------------------:|:---------------:|:---------------:|
| 1     | 616     | 686     | 720      | **AB** | 0 / 0 / 5           | AB              | 2 / 4 / 2       |
| 2     | 654     | 682     | 693      | **AB** | 0 / 1 / 4           | AB              | 0 / 5 / 2       |
| 3     | 635     | 716     | 744      | **A**  | 3 / 0 / 2           | A               | 1 / 4 / 2       |

Round 1 picked the right *direction* (observation-loop work) over a refactor.
Round 2 sharpened the *deliverable* (added redaction-at-write, dropped the premature v1/v2 split).
Round 3 sharpened the *shape*: from a 4-artifact program to a 1-PR experiment with a falsifiable cost metric.
The synthesis loop converged on substance in rounds 1-2; round 3's Author-A then *out-tightened the synthesizer*, which is the signal that further rounds would no longer be additive.

## Quality signals

- **Final-round judge consensus:** 3/5 = 0.6 (pragmatic / distributed-systems / perf judges picked A; tech-lead / long-lived-maintainer picked AB)
- **Disposition:** the split is informative — sprint-readiness-weighted judges want the experiment; strategy-weighted judges want the seam. The experiment unblocks the seam, not vice versa.
- **FATAL weaknesses retired across rounds:** 2 of 2 from round 1 (refactor-mislabel, pip-install contradiction). 1 new FATAL in round 3 (claim about Phase 2 gating being load-bearing was challenged but didn't shift the verdict).
- **Oscillation:** none (1 incumbent change across 3 rounds, below the 5-flip warning threshold).

## Composite reason_score

`quality_delta` (final 635 vs round-1 A 616) = +0.031 (modest — the win was in *shape*, not *volume*)
- quality_delta × 30 = 0.93
- rounds_survived × 5 = 15
- judge_consensus_final × 20 = 12
- critic_fatals_addressed × 15 = 30
- convergence_achieved (3-in-a-row) = 0
- no_oscillation = 5
- **reason_score = 63**

The score is moderate, not high — because the final round saw the incumbent flip rather than
converge. In `--mode convergent`, this would have triggered another round; in creative mode
bounded at 3, it indicates the answer is sharp but the loop did not stabilize, which is
informative on its own: the final candidate is concrete enough that further synthesis would
likely dilute it.

## What this changes downstream

- **Forces Phase 2 speculation** (`CLIVE_SPECULATE=1`) to wait on a concrete classifier-precision number, rather than ship behind a flag indefinitely
- **Defers** the privacy-sensitive opt-in decision-log feature (the round-2 incumbent's `~/.clive/decisions/`) until the bench surfaces a deficit it can't catch
- **Replaces** the `ClassifierStrategy` Protocol abstraction with a concrete second strategy (CmdAware) that, *if it wins*, makes the seam in round 4 obvious

## Files

- [BRIEF.md](BRIEF.md) — context every agent received
- [round{1,2,3}/](round1/) — per-round artifacts (candidates, critique, judges, label maps)
- [reason-lineage.jsonl](reason-lineage.jsonl) — machine-readable lineage
- [lineage.md](lineage.md) — human-readable round trace
- [candidates.md](candidates.md) — final-round A/B/AB side-by-side
- [judge-transcripts.md](judge-transcripts.md) — decoded judge reasoning
- [reason-results.tsv](reason-results.tsv) — per-round log
- [handoff.json](handoff.json) — chain schema (consumed by --chain predict)
