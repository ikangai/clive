# Promote the ScreenClassifier from a single regex sieve to a versioned, shadow-mode strategy with an A/B harness — measure first, replace later, on synthetic traffic only.

**Thesis:** The right next architectural step is to give `observation/observation.py` a *strategy seam* and a *shadow-mode evaluator* — not a production decision-log of real user sessions. Real-traffic logging of pane output cannot be made safe by a regex denylist (panes routinely surface `printenv`, GCP service-account JSON, JWTs, basic-auth URLs, customer IDs), and a counter-factual replay over recorded escalations cannot price unrun trajectories. The actionable architectural move is therefore to (a) define `ClassifierStrategy` as a protocol, (b) keep today's regex sieve as `LegacyStrategy`, (c) add a `ShadowStrategy` that runs in parallel and disagrees-but-doesn't-act, (d) drive both from the **existing synthetic corpus under `evals/observation/`** plus a small bank of frozen, hand-curated fixtures contributed by the project itself. No user pane content is ever persisted.

## Locus (verified)

- `src/clive/observation/observation.py:53-130` — `ScreenClassifier.classify()` is monolithic: a single method with four branches that each *unconditionally* set `needs_llm`. **All four** are escalation surfaces: `NEEDS_INPUT` (lines 62-69, fires on password/confirm prompts — the most common false-escalate in interactive flows), `ERROR` from intervention patterns (lines 70-79), non-zero exit (lines 92-100), and the `UNKNOWN` catch-all (lines 123-130). Any honest accounting of the escalation budget must touch all four.
- Call sites: `execution/interactive_runner.py` and `execution/toolcall_runner.py` instantiate `ScreenClassifier()` directly. There is no seam to swap in an alternative — today's design forecloses experimentation entirely.
- `evals/observation/` already contains scenarios, latency bench, metrics, and a published phase1 report. It is the natural home for a comparison harness; nothing about the architectural step requires capturing user data to exercise it.

## The move

1. **`observation/strategy.py`** — define `ClassifierStrategy(Protocol)` with `classify(screen, exit_code, *, last_command: str | None, app_type: str | None) -> ScreenEvent`. Carry `strategy_version: str` on `ScreenEvent`. The protocol explicitly takes the just-issued command and the pane's `app_type`, because the runner already has both inline at dispatch — withholding them from the classifier is the architectural error this step corrects.

2. **`LegacyStrategy`** — today's regex sieve, unchanged in behaviour, wrapped behind the protocol. Default in production. Zero behavioural risk.

3. **`ShadowStrategy`** — a candidate classifier (initial version: legacy + command-aware suppression rules, e.g. `git diff --quiet` exit-1 is informational, `grep` exit-1 is empty-match, `test`/`[` exit-1 is normal). Runs only when `CLIVE_CLASSIFIER_SHADOW=1`. Its decision is **discarded** in production; only the agree/disagree counter is incremented. No screen content stored.

4. **`evals/observation/ab_harness.py`** — replays the existing synthetic scenarios *and* a new `fixtures/` directory of frozen `(screen, exit_code, last_command, expected_event, expected_needs_llm)` cases written by maintainers from public examples (man pages, demo repos). Reports per-strategy: escalation rate, miss rate (events that should have escalated but didn't), token cost using `metrics.py:RunResult.cost_tokens`. Output stacks with `phase1-report.md`.

5. **Promotion rule, baked into the harness:** a strategy is promoted to default only if it strictly dominates Legacy on the frozen fixtures — equal or better miss rate at lower escalation rate. This is the gate the project lacks today.

## Rationale

The 60-80% token-reduction claim cited everywhere in the codebase rests on one regex function with no seam, no version, no comparison artifact, and four escalation branches that all hardcode `needs_llm=True`. Every downstream ambition — Phase 2 speculation default-on, BYOLLM economic claims, smaller per-pane models — gates on the classifier being *measurably* the right shape. A strategy seam plus a synthetic A/B is the smallest architectural change that unlocks all of them, and unlike a real-traffic log it has no privacy surface to defend.

## Tradeoff accepted

Synthetic + curated fixtures will under-represent the long tail of real terminal output. We will miss rare failure modes that only show up in live use. The compensating gain is that the harness ships *this* release without a security review blocker, the strategy seam permits any future researcher (including an autoresearch run) to propose and prove a v2 without touching call sites, and the promotion rule prevents a clever-looking strategy from regressing miss rate to win on escalation rate. Real-traffic measurement, if ever justified, becomes a *later* step layered on top of this seam — not a prerequisite for it.
