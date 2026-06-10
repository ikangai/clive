---
commit_hash: bc27c8b35556781ab85d437b688c69bfcde6b552
analyzed_at: 2026-05-17T22:45:00Z
scope: src/clive/observation/observation.py, src/clive/observation/byte_classifier.py, src/clive/execution/interactive_runner.py, src/clive/execution/toolcall_runner.py, evals/observation/**
files_analyzed: 8
---

## Key functions

| File | Function | Signature | Lines | Role |
|------|----------|-----------|-------|------|
| observation/observation.py | ScreenClassifier.classify | (screen: str, exit_code: int\|None=None) → ScreenEvent | 56-130 | Regex cascade. 6 branches. |
| observation/byte_classifier.py | ByteClassifier.feed | (chunk: bytes) → list[ByteEvent] | 54-79 | L2 *byte-stream* event detector (color, prompts, traceback, cmd_end). DIFFERENT subsystem from ScreenClassifier. |
| execution/interactive_runner.py | (in main loop) | — | 351-354 | Calls classify() ONLY when `exit_code == 0 and not detection.startswith("intervention:")`. |
| execution/toolcall_runner.py | (in main loop) | — | 244-248 | Calls classify() ONLY in `elif exit_code == 0:` branch. |
| evals/observation/metrics.py | aggregate | (runs: list[RunResult]) → ScenarioAgg | 34-49 | Per-mode median e2e_ms, missed_rate, cost_tokens. |
| evals/observation/scenarios.py | (module) | tuple of Scenario | 20-62 | 6 synthetic scenarios for L2 event-kind detection (color, prompts, spinners). |

## Classifier internal structure (observation.py:56-130)

Branch order in `classify()`:
1. INTERVENTION_PATTERNS check → NEEDS_INPUT or ERROR with `needs_llm=True`
2. `exit_code == 0` → SUCCESS, `needs_llm=False`
3. `exit_code != 0` → ERROR with `needs_llm=True` (the proposal's target)
4. PROGRESS_PATTERNS → RUNNING, `needs_llm=False`
5. `[AGENT_READY]` marker → SUCCESS, `needs_llm=False`
6. catch-all UNKNOWN → `needs_llm=True`

## Critical call-site discrepancy

The reason-loop proposal claims `observation.py:92-100` (Branch 3, non-zero exit) is "the dominant escalation surface for shell panes." However:

- **interactive_runner.py:351**: `if exit_code is not None and exit_code == 0 and not detection.startswith("intervention:"):` — classifier is invoked only on `exit_code == 0`.
- **toolcall_runner.py:242-248**: explicit `if exit_code != 0: parts.append("[EXIT:n] Command exited non-zero.")` — non-zero exits emit a synthesized message and bypass `classify()` entirely. The `elif exit_code == 0:` arm invokes classify().

**Implication:** Branch 3 of `classify()` is dead code from the runners' perspective. The actual escalation on non-zero exit happens at the runner level via the `[EXIT:n]` message that is then visible to the LLM next turn. Adding a `last_command` parameter to `classify()` and a `INFORMATIONAL_NONZERO` allowlist *will not affect anything* unless the runners are also modified to route non-zero exits through `classify()`. The proposal's "3 production files, 2 test/eval files" diff is incomplete as described.

## Eval harness mismatch

`evals/observation/scenarios.py` contains scenarios for **L2 byte-stream event detection** (color_alert, password_prompt, confirm_prompt, spinner_ok/fail, color_only) under modes `baseline | phase1 | phase2`. These measure *detection latency* and *missed-event rate* — not shell-command escalation rate or per-turn cost_tokens for shell-mode runs.

`metrics.py:RunResult` does have a `cost_tokens` field, but `aggregate()` produces a per-scenario `median_cost` over those L2-detection scenarios — not a fleet-level "median cost per shell-mode turn."

**Implication:** The proposal's success metric ("median `cost_tokens` for shell-mode runs drops ≥30%") cannot be measured by the existing bench without a new mode and a new scenario class. The proposal's claim that "Both numbers are already produced by aggregate() in evals/observation/metrics.py:34" is misleading — the numbers exist as fields but not as the right measurement.

## Scope mis-inclusion

`byte_classifier.py` is L2 byte-stream detection (color, prompts, errors mid-stream). It does not implement `ScreenClassifier` and has no `needs_llm` flag. The proposal targets `ScreenClassifier.classify()` in observation.py only. byte_classifier.py is in scope only as adjacent context.
