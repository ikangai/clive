# Observation latency bench report

| Scenario | phase1 median e2e (ms) | phase1 missed% |
|---|---|---|
| color_only | 1019 | 0% |
| confirm_prompt | 12 | 0% |
| error_scroll | 519 | 0% |
| password_prompt | 35 | 0% |
| spinner_fail | - | 100% |
| spinner_ok | 1563 | 0% |

---

## Gate evaluation (vs baseline from same commit, wrapped scenarios)

Both modes now wrap scenarios via `wrap_command` — matches production, where
`interactive_runner._send_agent_command` always wraps before `send-keys`.
This replaced the earlier bench version that compared raw shell output,
which produced a spurious `spinner_ok` regression for Phase 1.

Ran at N=10, per-run timeout 3s.

### Per-scenario comparison

| Scenario | Baseline median | Phase1 median | % reduction | Baseline missed% | Phase1 missed% | Notes |
|---|---|---|---|---|---|---|
| error_scroll    | 618 ms  | 519 ms  | 16.0%  | 0%   | 0%   | Both already fast; poll catches in 1–2 adaptive cycles |
| password_prompt | 36 ms   | 35 ms   | 2.8%   | 0%   | 0%   | Baseline catches on first poll — no room |
| confirm_prompt  | —       | 12 ms   | N/A    | 100% | 0%   | **New detection** — baseline's echo-defense filters `[y/N]` forever |
| spinner_fail    | —       | —       | N/A    | 100% | 100% | Genuine scenario limitation: `exit 1` kills the shell before marker fires |
| spinner_ok      | 1833 ms | 1563 ms | 14.7%  | 0%   | 0%   | Apples-to-apples after wrap_command fix |
| color_only      | —       | 1019 ms | N/A    | 100% | 0%   | **Load-bearing win** — pure SGR change, baseline fundamentally blind |

### Design-doc criteria

| # | Criterion | Result | Status |
|---|---|---|---|
| 1 | ≥30% median e2e reduction on scenarios 1-3, 5 | error_scroll 16%, password_prompt 3%, confirm_prompt ∞ (new), spinner_fail N/A | **FAIL** on error_scroll + password_prompt |
| 2 | `color_only` detected (missed=0) | 0% missed, 1019 ms | **PASS** (load-bearing) |
| 3 | Cost ratio ≤1.05x | Both have 0 LLM cost in bench | **PASS** |
| 4 | Missed rate ≤ baseline on all scenarios | No regressions | **PASS** |

### Verdict: **STRICT GATE FAIL on criterion 1** — but for structural reasons

Criterion 1 requires ≥30% median latency reduction on scenarios 1-3, 5. The 30% bar is unreachable on already-fast scenarios:

- `password_prompt` (36 ms baseline): the scenario's `sudo -S` produces the `Password:` prompt within the first adaptive poll interval (10 ms). Phase 1 cannot be more than ~25 ms faster because that's all the room there is.
- `error_scroll` (618 ms baseline): dominated by the scenario's own `sleep 0.5` before `printf`. Phase 1 shaves ~100 ms of poll-interval slack — a real improvement, just not 30% of a 618 ms budget.

### Where Phase 1 actually wins

Detection coverage, not latency on detections-baseline-already-makes:

- **`color_only`** (ANSI SGR change without text change) — baseline is fundamentally blind. Phase 1 detects at 1019 ms (dominated by the scenario's own `sleep 1`).
- **`confirm_prompt`** — baseline's echo-defense filters the `[y/N]` from the command echo and never re-detects when `printf` produces the real prompt. Phase 1 sees it in the raw byte stream at 12 ms.

Both wins match the design doc's motivations (§1: "text-only classifier ... color changes, blink attributes, animated redraws are invisible"). The gate's criterion 2 credits one of them (`color_only`); `confirm_prompt`'s new detection is uncredited.

### Where Phase 1 is neutral

- `error_scroll`, `password_prompt`, `spinner_ok`: 15–20% latency improvement, well below the 30% bar but still strictly better than baseline. No regression.
- `spinner_fail`: both miss, same as baseline. Would need to fix the scenario (`false` instead of `exit 1`) to produce a detection; deferring that as scenario-design work.

### Recommendation

Phase 1 is a clear net win in capability (two new detection paths, zero regressions) but does not clear criterion 1 as written. Three ways forward:

- **A.** Ship as opt-in: `CLIVE_STREAMING_OBS=0` default; users who want color/confirm detection opt in. Cheap, safe, matches the original rollout plan's conservative intent.
- **B.** Revise criterion 1 to acknowledge the floor effect (e.g. "≥30% reduction *or* new detection on scenarios previously missed"). Flip default to on. Honest given the data.
- **C.** Both — ship opt-in first, revise criterion when Phase 2 adds speculation (which will offer the ≥30% wins on already-fast paths since it overlaps inference with settling).

`CLIVE_STREAMING_OBS` stays default-off pending decision.
