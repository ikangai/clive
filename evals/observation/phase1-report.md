# Observation latency bench report

| Scenario | phase1 median e2e (ms) | phase1 missed% |
|---|---|---|
| color_only | 1023 | 0% |
| confirm_prompt | 12 | 0% |
| error_scroll | 518 | 0% |
| password_prompt | 34 | 0% |
| spinner_fail | - | 100% |
| spinner_ok | - | 100% |

---

## Gate evaluation (vs baseline from `83105db`)

### Per-scenario comparison

| Scenario | Baseline median | Phase1 median | % reduction | Baseline missed% | Phase1 missed% | Notes |
|---|---|---|---|---|---|---|
| error_scroll    | 661 ms  | 518 ms  | 21.6%  | 0%   | 0%   | Latency win below 30% bar |
| password_prompt | 36 ms   | 34 ms   | 5.6%   | 0%   | 0%   | Already fast; little room |
| confirm_prompt  | —       | 12 ms   | N/A    | 100% | 0%   | **New detection** — baseline's echo-defense filters `[y/N]`; Phase 1 sees it in raw bytes |
| spinner_fail    | —       | —       | N/A    | 100% | 100% | Both miss (shell `exit 1` kills session before any detectable signal) |
| spinner_ok      | 1869 ms | —       | —      | 0%   | **100%** | **Regression** (bench artifact — see below) |
| color_only      | —       | 1023 ms | N/A    | 100% | 0%   | **Load-bearing win** — pure SGR change, baseline fundamentally blind |

### Design-doc criteria (docs/plans/2026-04-16-streaming-observation-design.md §8.3)

| # | Criterion | Result | Status |
|---|---|---|---|
| 1 | ≥30% median e2e reduction on scenarios 1-3, 5 (error_scroll, password_prompt, confirm_prompt, spinner_fail) | error_scroll 21.6%, password_prompt 5.6%, confirm_prompt ∞ (new detection), spinner_fail N/A | **FAIL** on error_scroll + password_prompt |
| 2 | `color_only` detected (missed=0) | 0% missed, 1023 ms | **PASS** (load-bearing) |
| 3 | Cost ratio ≤1.05x | Both modes have 0 LLM cost in bench | **PASS** |
| 4 | Missed rate ≤ baseline on all scenarios | spinner_ok: 0% → 100% | **FAIL** on spinner_ok |

### Verdict: **STRICT GATE FAIL**

Phase 1 fails criteria 1 and 4 by the letter. But the failures have structural explanations that may warrant a gate revision, not a ship/no-ship decision.

### Root causes

**Criterion 1 near-misses on fast-already scenarios.** `password_prompt` (5.6%) and `error_scroll` (21.6%) are below the 30% bar mainly because both modes are already fast there — the poll loop's 10ms → 500ms adaptive backoff catches the target within one or two polls in most runs. The 30% bar may be better suited to *slow* paths where polling genuinely loses.

**Criterion 4 regression on `spinner_ok`.** Structural, not a real regression:
- Baseline uses a *shell prompt return* heuristic (`\n$ `, `bash-`) to detect command completion on a raw shell.
- Phase 1's `ByteClassifier.cmd_end` pattern only matches Clive's own `EXIT:<n> ___DONE_<hash>` marker from `wrap_command`.
- The bench **does not wrap** scenarios with `wrap_command`, so Phase 1 has no `cmd_end` event to fire.
- **In production**, `interactive_runner._send_agent_command` always calls `wrap_command(cmd, subtask.id)` before sending, so this "regression" does not occur in real subtask execution.

### Detection-coverage wins not captured by the gate

Phase 1 strictly extends what observation can see:
- **`color_only`** (ANSI SGR change without text change) — baseline is fundamentally blind (ANSI-stripped by `capture-pane -p`). Phase 1 detects at 1023 ms — same latency as baseline would need if it *could* see it, because the scenario's own `sleep 1` dominates.
- **`confirm_prompt`** — baseline's `send-keys` echo defense filters the `[y/N]` from the command echo and never re-detects when the target `printf` produces the real prompt. Phase 1 sees it in the raw byte stream at 12 ms.

Both wins are exactly the motivations in the design doc §1 ("Concrete problems: text-only classifier ... color changes, blink attributes, animated redraws are invisible"). Gate criterion 2 captures one of them; gate criteria 1/4 don't capture either.

### Recommendation

Three options for the human:

**A. Ship Phase 1 behind the flag (current state) with no default change.**
Flag stays `CLIVE_STREAMING_OBS=0` by default. Users who opt in get the detection wins. Defer gate passage until the bench better represents production (see option C).

**B. Accept the gate as revised.**
The detection wins are real and the only strict regression (`spinner_ok`) is bench-specific. Flip default to `CLIVE_STREAMING_OBS=1`. Document the bench caveat.

**C. Fix the bench to wrap scenarios with `wrap_command`, re-measure.**
`spinner_ok` and `spinner_fail` would detect via `cmd_end` in Phase 1 at roughly the same latency as baseline (possibly faster). Baseline's prompt-text heuristic becomes the fallback rather than the primary. Then re-run N=20 and re-evaluate the gate honestly.

My recommendation as the author of the code: **Option C**, then decide on A vs. B based on the revised numbers. The `spinner_ok` "regression" is a real finding about the bench, not about Phase 1 itself, and the gate should measure Phase 1 on the same semantic footing as production.

**Not taking any action yet.** `CLIVE_STREAMING_OBS` stays default-off. This report is the evidence base for the decision.
