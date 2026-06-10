# Predict Analysis — Command-aware ScreenClassifier (chained from reason loop 260517-2137)

**Date:** 2026-05-17 22:45
**Scope:** `src/clive/observation/observation.py`, `src/clive/observation/byte_classifier.py`, `src/clive/execution/interactive_runner.py`, `src/clive/execution/toolcall_runner.py`, `evals/observation/**`
**Personas:** 5 (Architecture Reviewer, Security Analyst, Performance Engineer, Reliability Engineer, Devil's Advocate)
**Debate Rounds:** 1 of 2 planned (narrow scope; anti-herd passed; second round marginal)
**Commit Hash:** bc27c8b35556781ab85d437b688c69bfcde6b552
**Anti-Herd Status:** PASSED (flip_rate ≈ 0.47, entropy ≈ 0.55)

## Summary

- **Total Findings:** 25 distinct after merging
  - Confirmed: 19 | Probable: 0 | Minority preserved: 6
- **Severity Breakdown:** Critical: 2 | High: 9 | Medium: 7 | Low: 1
- **Composite Score:** **predict_score = 333**
- **Verdict:** **NEEDS_REWORK before merge.** Direction survives; implementation shape needs re-specification.

## Top findings

1. **#1 (CRITICAL)** Branch 3 unreachable — runners gate `classify()` on `exit_code == 0`; proposal's locus is dead code.
2. **#2 (CRITICAL)** Merge-gate metric not computable — `cost_tokens=0` is hard-coded in the bench; existing scenarios test L2 byte-stream detection, not shell-mode classifier escalation.
3. **#3 (HIGH)** `shlex.split("git diff --quiet")[0] == "git"` — the proposal's own example does not match the proposed matcher.
4. **#4 (HIGH)** Compound commands with `;`/`&&`/`||`/`|` bypass first-token matching → weaponizable covert-channel.
5. **#5 (HIGH)** Allowlist must be `(command, exit_code_set)` pairs — `exit 128` (not-a-repo / fatal git error) must escalate; the proposal silences it.

## Anti-herd disposition

The 5/5 unanimous CRITICAL findings (#1 and #2) emerged independently from AR, PE, and DA in Phase 4 *before* any persona saw peer output — this is the highest-confidence consensus pattern. The 4/5 finding (#11, screen-content blindness under prompt-injection) is preserved because the dissenter (AR) only narrowed scope, did not dispute the core claim.

The minority findings (M-1 through M-6) are *preserved* rather than discarded — particularly DA-6 (Branch 6 UNKNOWN may be the dominant escalation drain) which is a structurally non-obvious challenge to the proposal's target selection. Worth re-checking in any follow-up.

## What this means for the reason-loop's converged candidate

The reason-loop final winner was a *direction* the swarm broadly endorses (reduce shell-mode escalation cost by adding command-awareness to the classification path). The *specific implementation shape* in `round3/candidate_A.md` has two CRITICAL gaps:

- The change as written touches a code path the runners do not reach. Either move the allowlist to the runner level (preferred — AR option b) or invert the runner guards (requires SA-7 audit first).
- The success metric cannot be computed by the existing eval harness. `cost_tokens` is hard-coded to 0 everywhere in `latency_bench.py`; `metrics.aggregate()` produces medians over scenarios that don't invoke `classify()`. Decouple the eval reform from the production PR.

Beyond the CRITICALs, 9 HIGH-severity refinements (matcher precision, compound-command rejection, (cmd, exit) pair allowlist, sandbox-aware guard, false-quiet metric, asymmetric merge gate, etc.) push the actual diff well past "5 files, ~25 LOC, no new module." The reason-loop's runner-up candidate (round 3 AB with the decision-log + replay-tool seam) is now a viable round-4 starting point IF the swarm-identified scope expansion is acknowledged.

## Files in this report

- [findings.md](findings.md) — 19 confirmed + 6 minority, ranked by priority_score
- [hypothesis-queue.md](hypothesis-queue.md) — testable hypotheses for optional `--chain debug` follow-up
- [persona-debates.md](persona-debates.md) — per-persona debate transcripts
- [predict-results.tsv](predict-results.tsv) — per-persona per-round counts
- [handoff.json](handoff.json) — machine-readable chain schema
- [codebase-analysis.md](codebase-analysis.md) — Phase 2 reconnaissance summary

## Reason → Predict empirical-evidence rule

Per workflow: downstream loop results **always override** swarm predictions. The predict findings are *priors*, not conclusions. Any actual implementation should:

1. Run the smallest possible change (single failing unit test → green) to verify finding #1's claim that Branch 3 is unreachable.
2. Run the existing bench (`python -m evals.observation.latency_bench --modes baseline phase1 --runs 5`) and verify that `cost_tokens` is 0 — confirming finding #2.

If those two empirical checks contradict the swarm, the swarm is wrong; act on the empirical result.
