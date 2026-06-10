# Ship a redacted classifier-decision log + counter-factual replay before any other observation work.

**Thesis:** The incumbent has the right *locus* (`src/clive/observation/observation.py`) and the right *forcing function* (the unmeasured 60-80% claim), but mis-shapes the deliverable. A v1/v2 strategy split is premature: there is no labelled corpus to choose between strategies yet, and the proposed "persistent screen capture" tradeoff is unacceptable as written because pane scrollback routinely contains secrets. The smaller, sharper next step is a **redacted, opt-out classifier-decision log + a counter-factual replay tool** that prices each escalation against what the main model actually did. v2 of the classifier is a *result* of this artifact, not a sibling of it.

## Locus (verified)

- `src/clive/observation/observation.py:53-130` — `ScreenClassifier.classify()` is the load-bearing function. Two branches unconditionally set `needs_llm=True`: non-zero exit (lines 92-100) and the `UNKNOWN` catch-all (lines 123-130). Every false-escalate here is a main-model call.
- Call sites: `execution/interactive_runner.py:352` and `execution/toolcall_runner.py:246`. Both already accumulate `total_pt`/`total_ct` (`interactive_runner.py:204,215,272,284,298`). The decision and its cost live next to each other already; nothing logs them paired.
- `evals/observation/{scenarios.py, latency_bench.py, metrics.py, phase1-report.md}` already exists. The incumbent under-credits this: `metrics.py:13` has `RunResult.cost_tokens`, `phase1-report.md` already publishes per-scenario missed-rate and reduction. What is missing is not the harness — it is the *real-traffic corpus*. Today's bench fires six synthetic shell one-liners; it cannot tell you that `git diff --quiet` exit-1 escalates wastefully in actual sessions.

## The smaller, sharper move

1. **`observation/decision_log.py`** — a thin wrapper that `interactive_runner` and `toolcall_runner` use to record, per `classify()` call: `{ts, session, pane_kind, exit_code, event_type, needs_llm, screen_sha256, screen_redacted_tail, post_decision: {llm_called, prompt_tokens, completion_tokens, model_tier, next_action ∈ {new_command, DONE, no-op-loop}}}`. The post-decision fields are filled at the *next* loop iteration — the runner already has the data inline. Written to `~/.clive/decisions/{session}.jsonl`. Off by default; `CLIVE_DECISION_LOG=1` to enable.

2. **Redaction at write time, not capture time.** A small allow/deny ruleset in `decision_log.py` strips `AKIA[0-9A-Z]{16}`, `ghp_[A-Za-z0-9]{36}`, `sk-[A-Za-z0-9]{32,}`, `[A-Za-z0-9+/]{40,}={0,2}` (base64 tokens), `BEGIN .* PRIVATE KEY`, and lines containing `export *` / `AWS_*` / `_TOKEN=` / `_KEY=`. The raw screen tail never leaves memory unredacted. This is what makes opt-in defensible — the incumbent's "persistent screen capture by default" cannot survive a security review when panes routinely show `kubectl logs`, `env`, and `printenv`.

3. **`evals/observation/replay.py`** — consume the JSONL, simulate alternative classifier behaviour (e.g. "don't escalate on `git diff --quiet` exit-1", "don't escalate on `grep` empty-match"), and emit `tokens_saved_if_kept_local` vs `turns_added_when_classifier_was_wrong`. Reuses `metrics.py:RunResult` so reports stack with the existing phase1 table.

4. **No v2 classifier yet.** v2 ships *after* replay shows which `UNKNOWN` and non-zero-exit subclasses dominate the waste. Building v1/v2 in parallel, as the incumbent proposes, picks a winner before the contest.

## Rationale vs. incumbent

- The incumbent claims the v2 classifier "consumes command-class from `command_extract.py`" — but `command_extract.py` parses the LLM's *outgoing* bash blocks, not the screen state being classified. That coupling is not free and may not even be the right signal. Decide after data.
- The incumbent's tradeoff line names privacy but waves it off with "tail-only window and per-session GC". Tail-window does not help: a 1KB tail of `aws sts get-session-token` output *is* the secret. Redaction-at-write is the only defensible posture.
- The forcing function the incumbent cites (Phase 2 speculation cannot default-on without escalation precision) is correct and survives in this version. Phase 2 still gates on this artifact.

## Tradeoff accepted

I accept a deliberately *narrower* artifact than the incumbent: opt-in, redacted, no strategy refactor yet. The cost is one more release cycle before v2 lands. The gain is (a) a security posture that survives `kubectl logs` in a pane, (b) a corpus shape forced by real traffic rather than guessed at by a v2 design, and (c) avoidance of the classic mistake of shipping a strategy interface before knowing the second strategy's signal.

This is the artifact that earns Phase 2 default-on, BYOLLM economic claims, and every future driver/mode change. It is one file plus two hooks plus a replay tool — not a parallel classifier.
