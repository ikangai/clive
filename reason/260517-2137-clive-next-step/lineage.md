# Round-by-round lineage

## Round 1 (cold start, no incumbent)

- **A** [616 words]: Collapse the flat-import shim layer in `src/clive/` and migrate every `from executor import ...` call site to dotted form.
- **Critique:** 2 FATAL — (i) refactor mislabelled as architectural step, (ii) the proposal explicitly *preserves* the `sys.path` injection it cites as motivation, making the pip-install argument bait-and-switch.
- **B** [686 words]: Replace `ScreenClassifier` with a versioned, replay-driven escalation policy. Capture real pane screens, build replay harness, refactor classifier as `v1` (regex) / `v2` (learned table). Connects to streaming Phase 2 gate.
- **AB** [720 words]: Take B's locus + forcing function, keep A's "forced now vs. forced later" framing discipline.
- **Judges (X=B, Y=AB, Z=A):** 5×Y → AB wins **5-0**. Runner-up: A×4, B×1. The discipline language carried as runner-up evidence.
- **Incumbent → AB.** consecutive_wins=1.

## Round 2 (incumbent = AB from round 1)

- **A** [654 words]: Same locus, sharper deliverable. *Strip the v1/v2 split* (premature without a corpus). Replace "persistent screen capture" with **redacted-at-write decision log** (`observation/decision_log.py`, regex strip of AWS keys / GH tokens / sk- / base64 / PRIVATE KEY lines). Off by default. Counter-factual replay tool reuses existing `metrics.py:RunResult`.
- **Critique:** 0 FATAL, 5 MAJOR, 2 MINOR — softer than round 1, indicating real improvement. Main attack: regex redaction is a known-leaky filter; opt-out vs. opt-in is hand-waved.
- **B** [682 words]: Independent rewrite addressing the same concerns.
- **AB** [693 words]: Synthesis.
- **Judges (X=A, Y=B, Z=AB):** 4×Z, 1×Y → **AB wins 4-1**. Judge 5 (long-lived-systems persona) preferred B for its independence from existing infra assumptions.
- **Incumbent → AB.** consecutive_wins=2.

## Round 3 (incumbent = round 2 AB)

- **A** [635 words]: Drops the decision-log + replay-tool + v1/v2-seam entirely. Reframes as **a single experiment**: thread `last_command` into `ScreenClassifier.classify()`, add `INFORMATIONAL_NONZERO` allowlist. Merge gate: ≥30% drop in median `cost_tokens` on existing `phase1` bench, no `missed_rate` increase. 3 production files, 2 test/eval files. No new module, no protocol, no env var.
- **Critique:** 1 FATAL, 4 MAJOR, 2 MINOR. The FATAL challenged the gating-on-30% claim ("why 30%? where's the calibration?") but did not invalidate the experiment.
- **B** [716 words]: Independent rewrite.
- **AB** [744 words]: Synthesis attempting to reconcile A's tightness with the incumbent's instrumentation breadth.
- **Judges (X=AB, Y=A, Z=B):** Y×3 (judges 1-3: pragmatic / dist-sys / perf), X×2 (judges 4-5: tech-lead / long-lived) → **A wins 3-2**. Runner-up: AB×3, A×2.
- **Incumbent flipped: AB → A.** consecutive_wins reset to 1.
- **Stop:** bounded iterations reached (3 of 3).

## Convergence interpretation

The loop did not produce 3 consecutive wins for the same candidate, so under `--mode convergent` this would have continued. In `--mode creative` bounded at 3 iterations, we accept the final-round winner as the surfaced answer. The pattern is informative: rounds 1-2 the synthesizer dominated; round 3 the synthesizer was *outperformed by its own input*, indicating that further synthesis would dilute rather than improve the answer.

The 3-2 split in the final round also reveals a real disposition divergence — the perf/dist-sys/pragmatic axis wants the falsifiable experiment now, while the tech-lead/long-lived-maintainer axis wants the slightly larger artifact (decision log + replay tool) for future leverage. Both are defensible. The reason loop's job was to surface the choice, not impose unanimity; the experiment is the move that makes the larger artifact's necessity decidable in 1-2 weeks.
