# Add a strategy seam to `ScreenClassifier` and feed it with a redacted, opt-in decision log replayed offline — seam first, corpus second, no v2 until data picks it.

[A is right that the 60-80% claim is unfalsifiable without real traffic and that synthetic fixtures cannot tell you `git diff --quiet` is wasteful in practice. B is right that there is no seam today and that a regex denylist alone is not a defensible privacy posture for arbitrary pane content. The synthesis is to ship both: the seam unlocks experimentation cheaply now, and an opt-in redacted log feeds it real signal later — but only the seam is the *architectural* step; the log is its sibling artifact, not a replacement.]

**Thesis:** Introduce `ClassifierStrategy` as a protocol in `observation/strategy.py` with `LegacyStrategy` wrapping today's regex sieve unchanged, and ship a `ShadowStrategy` plus an opt-in, redaction-at-write decision log that an offline replay tool prices against `metrics.py:RunResult.cost_tokens` — one architectural seam, two evidence sources (frozen fixtures + opt-in real traffic), one promotion rule.

## Locus (verified by both candidates)

- `src/clive/observation/observation.py:53-130` — `ScreenClassifier.classify()` is monolithic with four unconditional `needs_llm=True` branches: `NEEDS_INPUT` (62-69), `ERROR` (70-79), non-zero exit (92-100), `UNKNOWN` (123-130). All four are escalation surfaces.
- Call sites `execution/interactive_runner.py:352` and `execution/toolcall_runner.py:246` instantiate `ScreenClassifier()` directly. No seam exists; `total_pt`/`total_ct` already accumulate alongside (`interactive_runner.py:204,215,272,284,298`) but are never paired with the classifier decision that caused them.
- `evals/observation/{scenarios.py,latency_bench.py,metrics.py,phase1-report.md}` exists and is the natural home for the harness.

## The move

1. **`observation/strategy.py`** — `ClassifierStrategy(Protocol)` with `classify(screen, exit_code, *, last_command: str | None, app_type: str | None) -> ScreenEvent`, version-stamped via `strategy_version: str` on `ScreenEvent`. B is right that withholding `last_command` and `app_type` from the classifier — both already inline at the call site — is the architectural error this step corrects.

2. **`LegacyStrategy`** — today's regex sieve unchanged, default in production. Zero behavioural risk. Runner construction switches from `ScreenClassifier()` to a strategy factory.

3. **`ShadowStrategy`** — command-aware suppression (`git diff --quiet` exit-1 informational, `grep`/`test`/`[` exit-1 normal, etc.). Runs under `CLIVE_CLASSIFIER_SHADOW=1`; its decision is discarded in production and only the agree/disagree counter increments.

4. **`observation/decision_log.py`** — opt-in (`CLIVE_DECISION_LOG=1`, default off) JSONL at `~/.clive/decisions/{session}.jsonl` capturing `{ts, session, pane_kind, exit_code, event_type, needs_llm, strategy_version, screen_sha256, screen_redacted_tail, post_decision:{llm_called, prompt_tokens, completion_tokens, model_tier, next_action}}`. **Redaction at write time, not capture time**: strip `AKIA[0-9A-Z]{16}`, `ghp_[A-Za-z0-9]{36}`, `sk-[A-Za-z0-9]{32,}`, long base64, `BEGIN .* PRIVATE KEY`, lines matching `export *` / `AWS_*` / `_TOKEN=` / `_KEY=`. A is right that tail-windowing alone does not protect against `aws sts get-session-token`; the raw screen never leaves memory unredacted.

5. **`evals/observation/ab_harness.py` + `replay.py`** — harness runs the existing synthetic scenarios *and* a frozen `fixtures/` bank curated from public sources; `replay.py` consumes opt-in JSONL from real sessions and prices counter-factuals (`tokens_saved_if_kept_local`, `turns_added_when_classifier_was_wrong`). Both reuse `metrics.py:RunResult` so output stacks with `phase1-report.md`.

6. **Promotion rule, baked in:** a strategy is promoted to default only if it strictly dominates Legacy on the frozen fixtures (equal-or-better miss rate at lower escalation rate) **and** does not regress on whatever real-traffic JSONL has been contributed. The seam guarantees this rule has somewhere to attach.

## Resolving the contradiction

A claims B builds the strategy interface before knowing the second strategy's signal. B claims A's real-traffic logging cannot be made safe. Both critiques bite, but only B's is architectural: a *seam* without a second strategy is still the load-bearing unlock for Phase 2 speculation default-on, BYOLLM economics, and any autoresearch run on the classifier. A's critique of premature v2 is real, so we accept it: `ShadowStrategy` ships as a discard-only candidate, not a v2, and promotion gates on evidence. A's privacy critique is real, so the log is opt-in and redacted at write, not at read.

## Tradeoff accepted

We pay for two artifacts instead of one (~one file + two hooks each side) and accept that `ShadowStrategy`'s initial rules are educated guesses until the opt-in corpus accumulates. In exchange: the seam unblocks every downstream ambition immediately, the frozen fixtures give a security-review-clean default path, and the opt-in log gives a real-traffic ground truth without making pane capture the default posture. Phase 2 speculation default-on remains the forcing function — it gates on the promotion rule firing, not on either artifact existing in isolation.
