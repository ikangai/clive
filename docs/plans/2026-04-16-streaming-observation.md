# Streaming Observation & Speculative Decision — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace Clive's poll-based tmux observation with an event-driven FIFO pipeline (L1 heartbeat + L2 byte regex) and add version-stamped speculative LLM calls for intervention/error paths. Targets ≥50% latency reduction on intervention scenarios and makes ANSI color/blink signals detectable at all.

**Architecture:** Per-pane `pipe-pane` → FIFO → async reader. Raw bytes feed two cheap layers before L3 `ScreenClassifier`: L1 is a timestamp-only activity heartbeat; L2 is a small byte-regex set (ANSI SGR codes, keyword prompts, completion markers). High-confidence L2 triggers fan out to a `SpeculationScheduler` that fires the main LLM call early, with cancel-on-supersede semantics guaranteeing ordering. Behind feature flag `CLIVE_STREAMING_OBS=1`, off by default until phase gates pass.

**Tech stack:** Python 3.10+, asyncio, tmux `pipe-pane`, Anthropic SDK stream cancellation, pytest (with `pytest-asyncio` where needed).

**Design doc:** `docs/plans/2026-04-16-streaming-observation-design.md` (committed as `8c9fa28`)

**Key files / integration points:**
- `src/clive/observation/completion.py:36-80` — `wait_for_ready` (adds `event_source` param)
- `src/clive/observation/observation.py:53-130` — `ScreenClassifier` (unchanged, still L3)
- `src/clive/execution/interactive_runner.py:92-292` — `run_subtask_interactive` (adds spec watch + try_consume)
- `src/clive/session/session.py` — pane creation (wires `PaneStream`)
- `src/clive/models.py` — `PaneInfo` gets `stream: Optional[PaneStream] = None`

**Phase gates (see design doc §8.3):**
- Phase 1 ships iff: ≥30% median e2e latency reduction on scenarios 1-3,5; scenario 6 detected at all; cost ≤1.05x; missed rate ≤ baseline.
- Phase 2 ships iff: ≥50% median reduction on scenarios 1-3,5; cost ≤1.8x; missed rate ≤ baseline.

---

## Phase 0 — Baseline measurement harness

**Rationale:** Without a baseline we can't prove Phase 1 helps. Build the measurement tool first, run it against today's code, commit the numbers. Every subsequent phase gets compared to this artifact.

### Task 0.1: Scenario library

**Files:**
- Create: `evals/observation/__init__.py`
- Create: `evals/observation/scenarios.py`
- Test: `tests/test_observation_scenarios.py`

**Step 1: Write the failing test**

```python
# tests/test_observation_scenarios.py
"""Tests for observation latency benchmark scenarios."""
from evals.observation.scenarios import SCENARIOS, Scenario


def test_scenarios_has_all_six():
    assert len(SCENARIOS) == 6
    assert {s.id for s in SCENARIOS} == {
        "error_scroll", "password_prompt", "confirm_prompt",
        "spinner_ok", "spinner_fail", "color_only",
    }


def test_each_scenario_has_shell_command():
    for s in SCENARIOS:
        assert isinstance(s, Scenario)
        assert s.shell_command  # non-empty
        assert s.expected_l2_kinds  # non-empty tuple
        assert s.target_description  # non-empty for reporting


def test_color_only_is_marked_baseline_blind():
    # Scenario 6 baseline cannot detect — harness uses this flag to expect missed=True
    color_only = next(s for s in SCENARIOS if s.id == "color_only")
    assert color_only.baseline_blind is True

    error_scroll = next(s for s in SCENARIOS if s.id == "error_scroll")
    assert error_scroll.baseline_blind is False
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_observation_scenarios.py -v`
Expected: `ModuleNotFoundError: No module named 'evals.observation'`

**Step 3: Write minimal implementation**

```python
# evals/observation/__init__.py
```

```python
# evals/observation/scenarios.py
"""Synthetic scenarios for observation-layer latency benchmarking.

Each scenario is reproducible shell one-liner that generates a known
signal pattern. The bench harness runs each scenario N times per mode
(baseline/phase1/phase2) and reports detection latency, e2e latency,
and missed-event rate.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    shell_command: str
    expected_l2_kinds: tuple[str, ...]  # L2 event kinds expected to fire
    target_description: str             # for reports
    baseline_blind: bool = False        # true if today's loop fundamentally can't detect


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="error_scroll",
        shell_command=r"sleep 0.5 && printf '\x1b[31mERROR: boom\x1b[0m\n' && sleep 2",
        expected_l2_kinds=("color_alert", "error_keyword"),
        target_description="Red ERROR scrolls past quickly, no exit",
    ),
    Scenario(
        id="password_prompt",
        # `sudo -S` with a bogus non-existent command to force a password prompt without
        # escalating; -k invalidates cached creds so prompt definitely appears.
        shell_command="sudo -k && sudo -S echo pw_test 2>&1 || true",
        expected_l2_kinds=("password_prompt",),
        target_description="Password prompt mid-command",
    ),
    Scenario(
        id="confirm_prompt",
        shell_command="printf 'continue? [y/N] '",
        expected_l2_kinds=("confirm_prompt",),
        target_description="y/N confirmation prompt",
    ),
    Scenario(
        id="spinner_ok",
        shell_command="for i in 1 2 3 4 5; do printf '.'; sleep 0.3; done; echo done",
        expected_l2_kinds=("cmd_end",),
        target_description="Brief spinner-like activity then exit=0",
    ),
    Scenario(
        id="spinner_fail",
        shell_command="for i in 1 2 3; do printf '.'; sleep 0.3; done; exit 1",
        expected_l2_kinds=("cmd_end",),
        target_description="Spinner-like activity then exit=1",
    ),
    Scenario(
        id="color_only",
        # Print a line, then rewrite it with only color change — no new text.
        # Tests whether observation can see pure-SGR signals.
        shell_command=r"printf 'status\n'; sleep 1; printf '\x1b[A\x1b[31mstatus\x1b[0m\n'",
        expected_l2_kinds=("color_alert",),
        target_description="Color-only change to existing text (baseline can't detect)",
        baseline_blind=True,
    ),
)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_observation_scenarios.py -v`
Expected: 3 tests pass.

**Step 5: Commit**

```bash
git add evals/observation/__init__.py evals/observation/scenarios.py tests/test_observation_scenarios.py
git commit -m "feat(observation): latency-bench scenario library

Six reproducible shell scenarios stressing: red-error-scrolls-past,
password prompts, y/N confirms, spinner+exit, spinner+fail, and
color-only changes. baseline_blind flag marks scenarios today's
loop fundamentally cannot detect."
```

---

### Task 0.2: Metrics dataclass + reporter

**Files:**
- Create: `evals/observation/metrics.py`
- Test: `tests/test_observation_metrics.py`

**Step 1: Write the failing test**

```python
# tests/test_observation_metrics.py
"""Tests for observation bench metrics aggregation."""
import pytest
from evals.observation.metrics import RunResult, aggregate, format_markdown_report


def _result(latency, missed=False, cost=1000, spec_waste=None):
    return RunResult(
        scenario_id="error_scroll", mode="baseline",
        detect_latency_ms=None, e2e_latency_ms=latency,
        missed=missed, cost_tokens=cost, spec_waste=spec_waste,
    )


def test_aggregate_medians_and_missed_rate():
    runs = [_result(100), _result(200), _result(300), _result(400, missed=True)]
    agg = aggregate(runs)
    assert agg.median_e2e_ms == 250  # median of [100,200,300,400]
    assert agg.missed_rate == pytest.approx(0.25)
    assert agg.n == 4


def test_aggregate_excludes_missed_from_latency_median():
    # Missed runs have no latency measurement — exclude from median.
    runs = [_result(100), _result(200), _result(0, missed=True)]
    agg = aggregate(runs)
    assert agg.median_e2e_ms == 150  # median of [100, 200] — missed excluded


def test_markdown_report_includes_all_modes():
    rows = {
        "baseline": {"error_scroll": aggregate([_result(500)])},
        "phase1":   {"error_scroll": aggregate([_result(200)])},
    }
    md = format_markdown_report(rows)
    assert "baseline" in md
    assert "phase1" in md
    assert "error_scroll" in md
```

**Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_observation_metrics.py -v`
Expected: `ModuleNotFoundError` for `evals.observation.metrics`.

**Step 3: Implementation**

```python
# evals/observation/metrics.py
"""Metrics aggregation + markdown reporting for observation bench.

One RunResult per scenario-execution. Aggregate across N runs per
(scenario, mode) into ScenarioAgg, then emit a markdown comparison
table for the report.
"""
from dataclasses import dataclass
from statistics import median


@dataclass
class RunResult:
    scenario_id: str
    mode: str                              # baseline | phase1 | phase2
    detect_latency_ms: float | None        # None for baseline (no L2 stage)
    e2e_latency_ms: float                  # 0 when missed=True
    missed: bool
    cost_tokens: int
    spec_waste: float | None = None        # phase2 only


@dataclass
class ScenarioAgg:
    scenario_id: str
    mode: str
    n: int
    median_e2e_ms: float
    median_detect_ms: float | None
    missed_rate: float
    median_cost: float
    median_spec_waste: float | None


def aggregate(runs: list[RunResult]) -> ScenarioAgg:
    if not runs:
        raise ValueError("aggregate() requires at least one run")
    mode = runs[0].mode
    scenario_id = runs[0].scenario_id
    latencies = [r.e2e_latency_ms for r in runs if not r.missed]
    detect = [r.detect_latency_ms for r in runs if r.detect_latency_ms is not None]
    spec_waste = [r.spec_waste for r in runs if r.spec_waste is not None]
    return ScenarioAgg(
        scenario_id=scenario_id, mode=mode, n=len(runs),
        median_e2e_ms=median(latencies) if latencies else 0.0,
        median_detect_ms=median(detect) if detect else None,
        missed_rate=sum(1 for r in runs if r.missed) / len(runs),
        median_cost=median(r.cost_tokens for r in runs),
        median_spec_waste=median(spec_waste) if spec_waste else None,
    )


def format_markdown_report(rows: dict[str, dict[str, ScenarioAgg]]) -> str:
    # rows[mode][scenario_id] = ScenarioAgg
    modes = list(rows.keys())
    scenarios = sorted({sid for m in rows.values() for sid in m})
    lines = ["# Observation latency bench report\n"]
    header = "| Scenario | " + " | ".join(
        f"{m} median e2e (ms)" for m in modes
    ) + " | " + " | ".join(f"{m} missed%" for m in modes) + " |"
    sep = "|" + "|".join(["---"] * (1 + 2 * len(modes))) + "|"
    lines += [header, sep]
    for sid in scenarios:
        cells = [sid]
        cells += [f"{rows[m].get(sid).median_e2e_ms:.0f}" if sid in rows[m] else "-" for m in modes]
        cells += [f"{rows[m].get(sid).missed_rate*100:.0f}%" if sid in rows[m] else "-" for m in modes]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_observation_metrics.py -v`
Expected: 3 tests pass.

**Step 5: Commit**

```bash
git add evals/observation/metrics.py tests/test_observation_metrics.py
git commit -m "feat(observation): metrics dataclasses + markdown reporter"
```

---

### Task 0.3: Bench driver — baseline mode only

**Files:**
- Create: `evals/observation/latency_bench.py`
- Test: `tests/test_latency_bench.py`

This task ships **only the baseline mode**. Phase 1 mode is added in Task 1.7; phase 2 mode in Task 2.7.

**Step 1: Write the failing test**

```python
# tests/test_latency_bench.py
"""Tests for latency_bench driver.

Uses a tmux session on localhost; skipped if tmux not installed.
Baseline mode uses today's wait_for_ready poll path — exercises real
pane + real shell, measures real latency. Tests are slow (~1-2 min);
mark with pytest.mark.slow so CI can opt in.
"""
import shutil
import pytest
from evals.observation.latency_bench import run_scenario_baseline


pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux required")


@pytest.mark.slow
def test_baseline_error_scroll_returns_runresult():
    from evals.observation.scenarios import SCENARIOS
    scenario = next(s for s in SCENARIOS if s.id == "error_scroll")
    result = run_scenario_baseline(scenario)
    assert result.scenario_id == "error_scroll"
    assert result.mode == "baseline"
    assert result.e2e_latency_ms >= 0
    # error_scroll is NOT baseline_blind — we expect detection eventually.
    # (may take 500ms+ if poll catches it at a bad moment, but shouldn't miss.)


@pytest.mark.slow
def test_baseline_color_only_is_missed():
    from evals.observation.scenarios import SCENARIOS
    scenario = next(s for s in SCENARIOS if s.id == "color_only")
    result = run_scenario_baseline(scenario)
    # Baseline fundamentally cannot detect pure SGR changes.
    assert result.missed is True
```

**Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_latency_bench.py -v`
Expected: `ModuleNotFoundError` for `evals.observation.latency_bench`.

**Step 3: Implementation**

```python
# evals/observation/latency_bench.py
"""Latency benchmark driver for observation layer.

Each run:
  1. Spawn a fresh tmux session with a single shell pane.
  2. Instrument pane output via `pipe-pane` to a secondary "oracle"
     FIFO that records ground-truth timing independently of the code
     under test.
  3. Run the scenario's shell_command.
  4. Run the code under test (baseline = today's wait_for_ready loop).
  5. Compute e2e_latency_ms, detect_latency_ms (if applicable),
     missed (true iff code under test never saw the expected pattern).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from evals.observation.scenarios import Scenario, SCENARIOS
from evals.observation.metrics import RunResult, aggregate, format_markdown_report


def _oracle_fifo_path(run_id: str) -> str:
    p = f"/tmp/clive-bench/{run_id}-oracle.fifo"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        os.unlink(p)
    os.mkfifo(p)
    return p


def run_scenario_baseline(scenario: Scenario) -> RunResult:
    """Run a scenario against today's poll-based loop, measure latency."""
    run_id = uuid.uuid4().hex[:8]
    session = f"bench-{run_id}"
    oracle = _oracle_fifo_path(run_id)

    # Start a tmux session in the background with a shell in the single pane
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "bash"],
        check=True,
    )
    try:
        # Oracle pipe — captures ground truth of what the pane wrote, when.
        subprocess.run(
            ["tmux", "pipe-pane", "-t", f"{session}:0.0",
             f"cat > {oracle}"],
            check=True,
        )

        # TODO: Baseline observation: capture-pane in a poll loop, looking
        # for any of scenario.expected_l2_kinds' underlying text. Record
        # t0 = time command started, t_detect = time pattern seen, t_llm
        # = time our observation pipeline would have "handed off" to LLM
        # (for baseline: same as t_detect since there's no L2 stage).

        # See _run_baseline_loop below for the implementation.
        t0 = time.monotonic()
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{session}:0.0",
             scenario.shell_command, "Enter"],
            check=True,
        )
        t_detect, missed = _poll_for_baseline(session, scenario, start=t0, timeout=10.0)

        e2e_ms = (t_detect - t0) * 1000 if t_detect else 0.0
        return RunResult(
            scenario_id=scenario.id, mode="baseline",
            detect_latency_ms=None,
            e2e_latency_ms=e2e_ms,
            missed=missed,
            cost_tokens=0,  # no LLM calls in baseline measurement itself
        )
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session],
                       check=False, capture_output=True)
        if os.path.exists(oracle):
            os.unlink(oracle)


def _poll_for_baseline(session: str, scenario: Scenario, start: float, timeout: float):
    """Mimic today's wait_for_ready: capture-pane at adaptive 10→500ms backoff."""
    poll_interval = 0.010
    deadline = start + timeout
    # Map l2 kind → text substring to look for in capture-pane output
    text_targets = {
        "error_keyword": ["ERROR", "Traceback", "FATAL", "panic:"],
        "password_prompt": ["password:", "Password:"],
        "confirm_prompt": ["[y/N]", "[Y/n]"],
        "cmd_end": ["\n$", "\n# "],   # shell prompt reappears
        "color_alert": [],            # baseline cannot detect pure SGR
        "color_bg_alert": [],
        "blink_attr": [],
    }
    targets = []
    for k in scenario.expected_l2_kinds:
        targets.extend(text_targets.get(k, []))

    while time.monotonic() < deadline:
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", f"{session}:0.0", "-p"],
            capture_output=True, text=True, check=True,
        ).stdout
        for t in targets:
            if t in out:
                return time.monotonic(), False
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 0.5)
    return None, True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "phase1", "phase2"], required=True)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--out", default="evals/observation/report.json")
    args = ap.parse_args(argv)

    results: list[RunResult] = []
    for scenario in SCENARIOS:
        for i in range(args.runs):
            if args.mode == "baseline":
                results.append(run_scenario_baseline(scenario))
            else:
                raise NotImplementedError(f"mode={args.mode} lands in a later task")
            print(f"  {scenario.id} run {i+1}/{args.runs}", file=sys.stderr)

    rows: dict[str, dict[str, object]] = {args.mode: {}}
    by_scenario: dict[str, list[RunResult]] = {}
    for r in results:
        by_scenario.setdefault(r.scenario_id, []).append(r)
    for sid, runs in by_scenario.items():
        rows[args.mode][sid] = aggregate(runs)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"mode": args.mode, "runs": [r.__dict__ for r in results]}, f, indent=2)
    print(format_markdown_report(rows))


if __name__ == "__main__":
    main()
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_latency_bench.py -v -m slow`
Expected: both tests pass (slow, ~30s).

**Step 5: Commit**

```bash
git add evals/observation/latency_bench.py tests/test_latency_bench.py
git commit -m "feat(observation): latency bench — baseline mode

Drives synthetic scenarios against today's poll-based observation loop,
measures e2e detection latency via oracle pipe-pane FIFO. Phase 1/2
modes land in later tasks."
```

---

### Task 0.4: Capture baseline report

**Files:**
- Create: `evals/observation/baseline-report.md` (committed artifact)
- Create: `evals/observation/baseline-report.json` (committed artifact)

**Step 1:** Run the baseline:
```bash
python3 evals/observation/latency_bench.py --mode baseline --runs 20 \
  --out evals/observation/baseline-report.json \
  > evals/observation/baseline-report.md
```

(20 runs is enough to see the pattern without waiting all day; Phase 1/2 re-runs at 50 if needed.)

**Step 2:** Commit as-is. These are artifacts to compare Phase 1/2 against.

```bash
git add evals/observation/baseline-report.{md,json}
git commit -m "docs(observation): baseline latency measurements (N=20)"
```

**Phase 0 complete.** At this point we have a reproducible benchmark and numbers to beat. No core code has changed; tests still at 894 passing.

---

## Phase 1 — FIFO + L1/L2 + event-driven `wait_for_ready`

**Rationale:** Replace polling with events; add ANSI-level signal detection. Phase 1 is additive and feature-flagged — it can ship independently of Phase 2.

### Task 1.1: `ByteClassifier` (L2) — pure-function unit

**Files:**
- Create: `src/clive/observation/byte_classifier.py`
- Test: `tests/test_byte_classifier.py`

**Step 1: Write the failing test**

```python
# tests/test_byte_classifier.py
"""Tests for L2 byte-stream regex classifier."""
from byte_classifier import ByteClassifier, ByteEvent


def test_detects_red_fg():
    clf = ByteClassifier()
    events = clf.feed(b"\x1b[31mERROR\x1b[0m")
    kinds = [e.kind for e in events]
    assert "color_alert" in kinds


def test_detects_password_prompt():
    clf = ByteClassifier()
    events = clf.feed(b"Please enter password: ")
    assert any(e.kind == "password_prompt" for e in events)


def test_detects_yn_prompt():
    clf = ByteClassifier()
    events = clf.feed(b"Continue? [y/N] ")
    assert any(e.kind == "confirm_prompt" for e in events)


def test_detects_cmd_end_marker():
    clf = ByteClassifier()
    events = clf.feed(b"output line\nEXIT:0 ___DONE_abcd\n")
    assert any(e.kind == "cmd_end" for e in events)


def test_ignores_command_echo_with_dollar_guard():
    # When tmux echoes back the wrapping command, literal "EXIT:$?" appears.
    # Must not match.
    clf = ByteClassifier()
    events = clf.feed(b'echo "EXIT:$? ___DONE_abcd"\n')
    assert not any(e.kind == "cmd_end" for e in events)


def test_cross_chunk_pattern():
    # "password:" split across two feeds must still match.
    clf = ByteClassifier()
    events1 = clf.feed(b"passw")
    events2 = clf.feed(b"ord: ")
    kinds = [e.kind for e in events1 + events2]
    assert "password_prompt" in kinds


def test_does_not_fire_twice_for_same_match():
    # Same password prompt shouldn't fire twice when feed() is called again
    # with no new bytes.
    clf = ByteClassifier()
    clf.feed(b"Password: ")
    events2 = clf.feed(b"")
    assert not events2


def test_ring_buffer_bounded():
    clf = ByteClassifier()
    big = b"x" * (128 * 1024)
    clf.feed(big)
    # Should not blow memory; internal buffer stays bounded.
    assert len(clf._carryover) <= 128  # max pattern length
```

**Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_byte_classifier.py -v`
Expected: `ModuleNotFoundError: No module named 'byte_classifier'`

**Step 3: Implementation**

```python
# src/clive/observation/byte_classifier.py
"""L2 byte-stream classifier.

Scans raw tmux pane bytes (pre-render, with ANSI escapes intact) for
high-signal patterns: SGR alert colors, known prompts, error keywords,
and Clive's own command-end markers. Emits ByteEvent for each match.

Stateless across invocations except for:
  - _carryover: last (max_pattern_len - 1) bytes, to catch patterns
    split across chunk boundaries.
  - _last_match_pos: monotonic byte offset of the most recent match,
    to avoid double-firing when feed() is called with overlapping data.
"""
import re
import time
from dataclasses import dataclass


MAX_PATTERN_LEN = 128


BYTE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rb'\x1b\[[0-9;]*3[13]m'),               "color_alert"),
    (re.compile(rb'\x1b\[[0-9;]*4[13]m'),               "color_bg_alert"),
    (re.compile(rb'\x1b\[[0-9;]*5m'),                   "blink_attr"),
    (re.compile(rb'(?:^|[^\w])[Pp]assword\s*:'),        "password_prompt"),
    (re.compile(rb'\[y/N\]|\[Y/n\]'),                   "confirm_prompt"),
    (re.compile(rb'Are you sure'),                      "confirm_prompt"),
    (re.compile(rb'Traceback|FATAL|panic:'),            "error_keyword"),
    (re.compile(rb'Permission denied'),                 "permission_error"),
    (re.compile(rb'EXIT:\d+ ___DONE_'),                 "cmd_end"),
]

# Echoes of wrap_command contain "EXIT:$" (unexpanded). Excluded below.
_CMD_ECHO_GUARD = re.compile(rb'EXIT:\$')


@dataclass
class ByteEvent:
    kind: str
    match_bytes: bytes
    # Monotonic offset into the pane's byte stream where match *starts*.
    stream_offset: int
    # Wall-clock when observed (for latency measurements).
    timestamp: float


class ByteClassifier:
    def __init__(self):
        self._carryover = b""
        self._stream_pos = 0       # monotonic: total bytes seen
        self._last_emitted_pos = -1

    def feed(self, chunk: bytes) -> list[ByteEvent]:
        if not chunk and not self._carryover:
            return []
        # Scan window: carryover + new chunk
        window = self._carryover + chunk
        # Offset in the stream where window[0] lives
        window_base = self._stream_pos - len(self._carryover)
        events: list[ByteEvent] = []

        for pattern, kind in BYTE_PATTERNS:
            for m in pattern.finditer(window):
                abs_pos = window_base + m.start()
                if abs_pos <= self._last_emitted_pos:
                    continue
                # cmd_end guard: reject if this is a command echo
                if kind == "cmd_end":
                    if _CMD_ECHO_GUARD.search(window, max(0, m.start() - 16), m.end()):
                        continue
                events.append(ByteEvent(
                    kind=kind,
                    match_bytes=m.group(0),
                    stream_offset=abs_pos,
                    timestamp=time.monotonic(),
                ))
                self._last_emitted_pos = max(self._last_emitted_pos, abs_pos)

        # Advance stream pos and carry over the tail
        self._stream_pos += len(chunk)
        tail_len = min(MAX_PATTERN_LEN - 1, len(window))
        self._carryover = window[-tail_len:] if tail_len else b""
        return events
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_byte_classifier.py -v`
Expected: all 8 tests pass.

**Step 5: Commit**

```bash
git add src/clive/observation/byte_classifier.py tests/test_byte_classifier.py
git commit -m "feat(observation): L2 byte-stream classifier

Scans raw pane bytes for ANSI SGR alerts, password/confirm prompts,
error keywords, and wrap_command completion markers. Handles
cross-chunk patterns via 128-byte carryover. Dedups via monotonic
stream-offset tracking."
```

---

### Task 1.2: `PaneStream` — FIFO lifecycle + async reader

**Files:**
- Create: `src/clive/observation/fifo_stream.py`
- Test: `tests/test_fifo_stream.py`

**Step 1: Write the failing test**

```python
# tests/test_fifo_stream.py
"""Integration tests for PaneStream — uses real mkfifo + subprocess writer."""
import asyncio
import os
import pytest
import tempfile

from fifo_stream import PaneStream


@pytest.fixture
def fifo_path():
    d = tempfile.mkdtemp(prefix="clive-fifo-test-")
    p = os.path.join(d, "test.fifo")
    yield p
    if os.path.exists(p):
        os.unlink(p)
    os.rmdir(d)


@pytest.mark.asyncio
async def test_read_loop_emits_events_to_subscriber(fifo_path):
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    q = stream.subscribe()

    # Write bytes in another task
    async def write():
        await asyncio.sleep(0.05)
        # Open for write non-blocking via a subprocess to avoid deadlock
        import subprocess
        subprocess.run(
            ["bash", "-c", f'printf "\\x1b[31mERROR\\x1b[0m" > {fifo_path}'],
            check=True,
        )

    writer = asyncio.create_task(write())
    # Read with timeout
    event = await asyncio.wait_for(q.get(), timeout=2.0)
    assert event.kind == "color_alert"
    await writer
    await stream.close()


@pytest.mark.asyncio
async def test_activity_heartbeat_updates(fifo_path):
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    t_before = stream.last_byte_ts

    import subprocess
    await asyncio.sleep(0.01)
    subprocess.run(["bash", "-c", f'echo hi > {fifo_path}'], check=True)
    # Give reader a moment
    await asyncio.sleep(0.1)

    assert stream.last_byte_ts > t_before
    await stream.close()


@pytest.mark.asyncio
async def test_close_cancels_reader(fifo_path):
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    await stream.close()
    assert stream._reader_task.done()
```

**Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_fifo_stream.py -v`
Expected: `ModuleNotFoundError: No module named 'fifo_stream'`

(Note: `pytest-asyncio` must be installed. If not: `pip install pytest-asyncio --break-system-packages` and add `asyncio_mode = "auto"` to pytest config, or use `@pytest.mark.asyncio` as we do.)

**Step 3: Implementation**

```python
# src/clive/observation/fifo_stream.py
"""Per-pane FIFO byte stream with async reader + L2 classifier + fan-out.

Lifecycle:
  create()    → mkfifo, start reader coroutine
  subscribe() → register an asyncio.Queue, get ByteEvents via it
  close()     → signal reader, drain queues, unlink FIFO

The reader opens the FIFO in non-blocking mode so close() doesn't
deadlock waiting for a writer. Each chunk read is passed to the
ByteClassifier; resulting events are fanned out to all subscribers.

For tmux integration, the caller is responsible for running
`tmux pipe-pane -o 'cat > <fifo_path>'`. PaneStream.from_fifo_path
gives you the reader side only.
"""
import asyncio
import logging
import os
import time

from byte_classifier import ByteClassifier, ByteEvent

log = logging.getLogger(__name__)

_CHUNK_SIZE = 4096
_SUBSCRIBER_QUEUE_SIZE = 256


class PaneStream:
    def __init__(self, fifo_path: str):
        self.fifo_path = fifo_path
        self.classifier = ByteClassifier()
        self.last_byte_ts = time.monotonic()
        self.subscribers: list[asyncio.Queue] = []
        self._closed = False
        self._reader_task: asyncio.Task | None = None

    @classmethod
    def from_fifo_path(cls, fifo_path: str) -> "PaneStream":
        assert os.path.exists(fifo_path), fifo_path
        self = cls(fifo_path)
        self._reader_task = asyncio.create_task(self._read_loop())
        return self

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self.subscribers.append(q)
        return q

    async def _read_loop(self):
        # Open non-blocking so close() can cancel us without a writer present.
        fd = os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            loop = asyncio.get_running_loop()
            while not self._closed:
                try:
                    chunk = os.read(fd, _CHUNK_SIZE)
                except BlockingIOError:
                    await asyncio.sleep(0.005)
                    continue
                if not chunk:
                    # FIFO EOF (writer closed). Keep re-reading; a new
                    # writer may attach. Sleep to avoid busy loop.
                    await asyncio.sleep(0.02)
                    continue
                self.last_byte_ts = time.monotonic()
                events = self.classifier.feed(chunk)
                for ev in events:
                    for q in self.subscribers:
                        try:
                            q.put_nowait(ev)
                        except asyncio.QueueFull:
                            log.warning(
                                "subscriber queue full, dropping %s event",
                                ev.kind,
                            )
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_fifo_stream.py -v`
Expected: 3 tests pass.

**Step 5: Commit**

```bash
git add src/clive/observation/fifo_stream.py tests/test_fifo_stream.py
git commit -m "feat(observation): PaneStream — FIFO lifecycle + async reader

Non-blocking read loop feeds ByteClassifier and fans out events to
subscriber queues. Heartbeat timestamp updated on every chunk for
L1 activity detection. Bounded subscriber queues drop oldest on
backpressure."
```

---

### Task 1.3: Event-driven `wait_for_ready`

**Files:**
- Modify: `src/clive/observation/completion.py:36-80`
- Test: `tests/test_wait_for_ready_events.py`

**Step 1: Write the failing test**

```python
# tests/test_wait_for_ready_events.py
"""Tests for event-driven wait_for_ready path."""
import asyncio
import pytest
from unittest.mock import MagicMock

from byte_classifier import ByteEvent
from completion import wait_for_ready
from models import PaneInfo


def _fake_pane(screen_content: str):
    pane = MagicMock()
    pane.cmd.return_value.stdout = screen_content.splitlines()
    return pane


@pytest.mark.asyncio
async def test_returns_on_cmd_end_event():
    pane = _fake_pane("output\nEXIT:0 ___DONE_abc\n[AGENT_READY] $ ")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell")
    q = asyncio.Queue()
    q.put_nowait(ByteEvent(kind="cmd_end", match_bytes=b"EXIT:0 ___DONE_abc",
                           stream_offset=0, timestamp=0.0))

    screen, method = wait_for_ready(
        info, marker="___DONE_abc", event_source=q, max_wait=2.0,
    )
    assert method == "marker"
    assert "EXIT:0" in screen


@pytest.mark.asyncio
async def test_returns_on_intervention_event():
    pane = _fake_pane("sudo -S\nPassword: ")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell")
    q = asyncio.Queue()
    q.put_nowait(ByteEvent(kind="password_prompt", match_bytes=b"Password: ",
                           stream_offset=0, timestamp=0.0))

    screen, method = wait_for_ready(
        info, event_source=q, detect_intervention=True, max_wait=2.0,
    )
    assert method == "intervention:password_prompt"


@pytest.mark.asyncio
async def test_idle_timeout_when_no_events():
    pane = _fake_pane("still thinking...")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell",
                    idle_timeout=0.2)
    q = asyncio.Queue()
    screen, method = wait_for_ready(info, event_source=q, max_wait=0.5)
    assert method in ("idle", "max_wait")
```

**Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_wait_for_ready_events.py -v`
Expected: TypeError — `wait_for_ready` doesn't accept `event_source`.

**Step 3: Implementation**

Modify `src/clive/observation/completion.py`. Add `event_source` param to `wait_for_ready`. New path `_wait_event_driven`. Map `intervention-kind` byte events to `intervention:<type>` detection strings consistent with existing codebase.

```python
# In completion.py, extend wait_for_ready signature:
def wait_for_ready(
    pane_info: PaneInfo,
    marker: str | None = None,
    timeout: float | None = None,
    max_wait: float = MAX_WAIT,
    detect_intervention: bool = False,
    event_source: "asyncio.Queue | None" = None,
) -> tuple[str, str]:
    if event_source is None:
        return _wait_polling(pane_info, marker, timeout, max_wait, detect_intervention)
    return _wait_event_driven(
        pane_info, marker, event_source, timeout, max_wait, detect_intervention,
    )
```

Extract existing body into `_wait_polling` (pure refactor — no behaviour change). Add `_wait_event_driven`:

```python
_INTERVENTION_KINDS = {
    "password_prompt": "password_prompt",
    "confirm_prompt":  "confirmation_prompt",
    "permission_error": "permission_error",
}

def _wait_event_driven(pane_info, marker, event_source, timeout, max_wait, detect_intervention):
    import asyncio
    idle = timeout or pane_info.idle_timeout or DEFAULT_IDLE_TIMEOUT
    start = time.time()
    loop = asyncio.get_event_loop()

    async def _await():
        last_event_ts = time.monotonic()
        while True:
            remaining = max_wait - (time.time() - start)
            if remaining <= 0:
                return "max_wait"
            try:
                evt = await asyncio.wait_for(event_source.get(), timeout=idle)
            except asyncio.TimeoutError:
                # No events for idle seconds -> treat as idle completion
                return "idle"
            if marker and evt.kind == "cmd_end" and marker.encode() in evt.match_bytes:
                return "marker"
            if detect_intervention and evt.kind in _INTERVENTION_KINDS:
                return f"intervention:{_INTERVENTION_KINDS[evt.kind]}"
            last_event_ts = time.monotonic()

    method = loop.run_until_complete(_await()) if not loop.is_running() else \
             asyncio.run_coroutine_threadsafe(_await(), loop).result(max_wait + 1)

    # One final capture for the caller
    lines = pane_info.pane.cmd("capture-pane", "-p", "-J").stdout
    screen = "\n".join(lines) if lines else ""
    return screen, method
```

**Note on sync/async bridging:** the existing `wait_for_ready` is called synchronously from `interactive_runner`. When `event_source` is provided, we need an asyncio loop. Options are documented in Task 1.4; for now, the function uses `loop.run_until_complete` if no loop is running (test case) or `run_coroutine_threadsafe` if one is (runner path).

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_wait_for_ready_events.py -v`
Expected: 3 tests pass.

Also run full suite to confirm refactor didn't break anything:
Run: `python3 -m pytest tests/ --tb=no -q`
Expected: still 894+ passing (existing tests hit `_wait_polling` via unchanged poll path).

**Step 5: Commit**

```bash
git add src/clive/observation/completion.py tests/test_wait_for_ready_events.py
git commit -m "feat(observation): event-driven path in wait_for_ready

When event_source queue is provided, block on ByteEvents instead of
polling capture-pane. Maps password_prompt/confirm_prompt events to
existing 'intervention:<type>' return codes. Poll path preserved as
fallback (unchanged behaviour when event_source is None)."
```

---

### Task 1.4: Pane lifecycle wiring + per-pane event loop

**Files:**
- Modify: `src/clive/models.py` — add `stream: Optional[PaneStream]` to `PaneInfo`
- Modify: `src/clive/session/session.py` — create `PaneStream` on pane init, teardown on close
- Test: `tests/test_pane_stream_lifecycle.py`

**Step 1: Write the failing test**

```python
# tests/test_pane_stream_lifecycle.py
"""Tests that PaneInfo.stream is attached when CLIVE_STREAMING_OBS=1."""
import os
import pytest
from unittest.mock import patch

from session import create_pane


def test_stream_attached_when_flag_set(monkeypatch):
    monkeypatch.setenv("CLIVE_STREAMING_OBS", "1")
    # ...build a minimal fake tmux session + create a pane...
    # (exact fixture depends on how session.py is structured; use
    # existing test_session.py fixtures as templates.)
    pane = create_pane(...)  # adapt to real signature
    assert pane.stream is not None


def test_stream_not_attached_when_flag_unset(monkeypatch):
    monkeypatch.delenv("CLIVE_STREAMING_OBS", raising=False)
    pane = create_pane(...)
    assert pane.stream is None
```

(Use patterns from existing `tests/test_session*.py` for the fixture — I leave this task to whoever picks it up; the exact API varies.)

**Step 2: Run to verify it fails**

Expected: `AttributeError` — `PaneInfo` has no `stream` field.

**Step 3: Implementation**

a) Extend `models.py`:

```python
# models.py — extend PaneInfo
@dataclass
class PaneInfo:
    # ...existing fields...
    stream: "PaneStream | None" = None
```

b) Update `session.py` pane creation flow. Rough shape — adapt to the real function signature:

```python
def _maybe_attach_stream(pane_info: PaneInfo, session_id: str) -> None:
    if os.environ.get("CLIVE_STREAMING_OBS") != "1":
        return
    try:
        fifo_dir = f"/tmp/clive/{session_id}/pipes"
        os.makedirs(fifo_dir, exist_ok=True)
        fifo_path = f"{fifo_dir}/{pane_info.name}.fifo"
        if os.path.exists(fifo_path):
            os.unlink(fifo_path)
        os.mkfifo(fifo_path)
        pane_info.pane.cmd("pipe-pane", "-o", f"cat > {fifo_path}")

        # Bind to the pane's asyncio loop (see Task 1.5 for loop mgmt)
        from fifo_stream import PaneStream
        pane_info.stream = _run_on_pane_loop(
            lambda: PaneStream.from_fifo_path(fifo_path)
        )
    except (OSError, FileNotFoundError) as e:
        log.warning("stream setup failed for pane %s: %s", pane_info.name, e)
        pane_info.stream = None
```

c) On pane teardown: `pipe-pane` off, call `await stream.close()`, unlink FIFO.

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_pane_stream_lifecycle.py tests/ --tb=no -q`
Expected: new tests pass, existing 894+ unaffected.

**Step 5: Commit**

```bash
git add src/clive/models.py src/clive/session/session.py tests/test_pane_stream_lifecycle.py
git commit -m "feat(observation): wire PaneStream into pane lifecycle

Behind CLIVE_STREAMING_OBS=1: create FIFO + pipe-pane + PaneStream
at pane init, tear down on pane close. Unset flag keeps pane_info.stream
as None so downstream code takes the poll path."
```

---

### Task 1.5: Per-pane asyncio loop (background thread)

**Files:**
- Create: `src/clive/execution/pane_loop.py`
- Test: `tests/test_pane_loop.py`

**Context:** `interactive_runner` is synchronous. The FIFO reader + event queue + (later) speculation scheduler are async. Simplest bridge: each pane gets a dedicated asyncio loop running on a background thread. Sync code pushes work via `asyncio.run_coroutine_threadsafe()`.

**Step 1: Write the failing test**

```python
# tests/test_pane_loop.py
import asyncio
from pane_loop import PaneLoop


def test_submit_and_get_result():
    loop = PaneLoop.start()
    try:
        async def work():
            await asyncio.sleep(0.01)
            return 42
        fut = loop.submit(work())
        assert fut.result(timeout=1.0) == 42
    finally:
        loop.stop()


def test_loop_stops_cleanly():
    loop = PaneLoop.start()
    loop.stop()
    assert not loop.thread.is_alive()


def test_submit_after_stop_raises():
    import pytest
    loop = PaneLoop.start()
    loop.stop()
    with pytest.raises(RuntimeError):
        loop.submit(asyncio.sleep(0))
```

**Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_pane_loop.py -v`
Expected: `ModuleNotFoundError: No module named 'pane_loop'`

**Step 3: Implementation**

```python
# src/clive/execution/pane_loop.py
"""Per-pane asyncio loop on a background thread.

Bridges the synchronous interactive_runner to the async observation
pipeline. Each pane gets its own loop so panes can't starve each
other. Submit coroutines via PaneLoop.submit(); get concurrent.futures.Future.
"""
import asyncio
import threading
from concurrent.futures import Future


class PaneLoop:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stopped = False
        self.thread: threading.Thread | None = None

    @classmethod
    def start(cls) -> "PaneLoop":
        self = cls()
        self.thread = threading.Thread(target=self._run, daemon=True, name="clive-pane-loop")
        self.thread.start()
        self._ready.wait(timeout=2.0)
        return self

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def submit(self, coro) -> Future:
        if self._stopped or self._loop is None:
            raise RuntimeError("PaneLoop is stopped")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 2.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self.thread:
            self.thread.join(timeout=timeout)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_pane_loop.py -v`
Expected: 3 tests pass.

**Step 5: Commit**

```bash
git add src/clive/execution/pane_loop.py tests/test_pane_loop.py
git commit -m "feat(execution): per-pane asyncio loop on background thread

Bridge between sync interactive_runner and async observation/speculation
layers. Each pane gets its own loop so panes can't starve each other."
```

---

### Task 1.6: Interactive runner consumes events (Phase 1 integration)

**Files:**
- Modify: `src/clive/execution/interactive_runner.py:77-90` — `_send_agent_command` passes `event_source` when stream exists
- Test: `tests/test_interactive_runner_streaming.py`

**Step 1: Write the failing test**

```python
# tests/test_interactive_runner_streaming.py
"""Integration test: streaming-observation path reaches wait_for_ready."""
from unittest.mock import MagicMock, patch

from interactive_runner import _send_agent_command
from models import Subtask, PaneInfo


def test_send_command_uses_event_source_when_stream_present(monkeypatch):
    pane = MagicMock()
    stream = MagicMock()
    stream.subscribe.return_value = "FAKE_QUEUE"
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell",
                    stream=stream)
    subtask = Subtask(id="t1", description="test", ...)  # fill real dataclass

    with patch("interactive_runner.wait_for_ready") as wfr:
        wfr.return_value = ("screen", "marker")
        _send_agent_command("echo hi", subtask, info, "/tmp/clive/test")

    # Asserted: wait_for_ready was invoked with event_source kwarg
    args, kwargs = wfr.call_args
    assert kwargs.get("event_source") is not None


def test_send_command_no_event_source_when_stream_absent():
    pane = MagicMock()
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell",
                    stream=None)
    subtask = Subtask(id="t1", description="test", ...)

    with patch("interactive_runner.wait_for_ready") as wfr:
        wfr.return_value = ("screen", "marker")
        _send_agent_command("echo hi", subtask, info, "/tmp/clive/test")

    args, kwargs = wfr.call_args
    assert kwargs.get("event_source") is None
```

**Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_interactive_runner_streaming.py -v`
Expected: fail — runner doesn't pass `event_source` yet.

**Step 3: Implementation**

Modify `_send_agent_command` (line 77) — if `pane_info.stream` is not `None`, subscribe and pass the queue.

```python
def _send_agent_command(cmd, subtask, pane_info, session_dir):
    if pane_info.app_type in _SHELL_LIKE_APP_TYPES:
        cmd = _wrap_for_sandbox(cmd, session_dir, sandboxed=pane_info.sandboxed)
    wrapped, marker = wrap_command(cmd, subtask.id)
    pane_info.pane.send_keys(wrapped, enter=True)
    event_source = pane_info.stream.subscribe() if pane_info.stream else None
    screen, method = wait_for_ready(
        pane_info, marker=marker, detect_intervention=True,
        event_source=event_source,
    )
    return screen, method
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_interactive_runner_streaming.py tests/ --tb=no -q`
Expected: new tests pass, existing 894+ unaffected.

**Step 5: Commit**

```bash
git add src/clive/execution/interactive_runner.py tests/test_interactive_runner_streaming.py
git commit -m "feat(execution): runner passes event_source to wait_for_ready

When pane has a stream (CLIVE_STREAMING_OBS=1), subscribe and pass the
event queue through. Without a stream, falls back to poll path.
Bit-identical to today when flag unset."
```

---

### Task 1.7: `latency_bench.py` — phase1 mode

**Files:**
- Modify: `evals/observation/latency_bench.py` — add `run_scenario_phase1`
- Test: update `tests/test_latency_bench.py`

**Step 1: Write the failing test**

```python
# Add to tests/test_latency_bench.py
@pytest.mark.slow
def test_phase1_color_only_is_detected():
    """Phase 1's load-bearing test: baseline can't see pure SGR changes,
    phase 1 must."""
    from evals.observation.scenarios import SCENARIOS
    from evals.observation.latency_bench import run_scenario_phase1
    scenario = next(s for s in SCENARIOS if s.id == "color_only")
    result = run_scenario_phase1(scenario)
    assert result.missed is False
    assert result.detect_latency_ms is not None
    assert result.e2e_latency_ms > 0


@pytest.mark.slow
def test_phase1_beats_baseline_on_error_scroll():
    from evals.observation.scenarios import SCENARIOS
    from evals.observation.latency_bench import run_scenario_baseline, run_scenario_phase1
    scenario = next(s for s in SCENARIOS if s.id == "error_scroll")
    base = [run_scenario_baseline(scenario).e2e_latency_ms for _ in range(5)]
    p1 = [run_scenario_phase1(scenario).e2e_latency_ms for _ in range(5)]
    assert min(p1) < max(base)  # at worst, fastest p1 beats slowest baseline
```

**Step 2: Run to verify failure**

Expected: `ImportError: cannot import name 'run_scenario_phase1'`.

**Step 3: Implementation**

Add `run_scenario_phase1` to `latency_bench.py`. It runs the same scenario but with `CLIVE_STREAMING_OBS=1` and observes via a `PaneStream` directly (not the full runner — we're measuring the observation layer, not end-to-end LLM turn). Hook timing on L2 event arrival.

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_latency_bench.py -v -m slow`
Expected: both new tests pass.

**Step 5: Commit**

```bash
git add evals/observation/latency_bench.py tests/test_latency_bench.py
git commit -m "feat(observation): latency_bench phase1 mode"
```

---

### Task 1.8: Phase 1 gate check

Run `python3 evals/observation/latency_bench.py --mode phase1 --runs 50 --out evals/observation/phase1-report.json > evals/observation/phase1-report.md`.

Compare `phase1-report.md` against `baseline-report.md`. Phase 1 ships iff:

- Median `e2e_latency_ms` reduction ≥30% on scenarios `error_scroll`, `password_prompt`, `confirm_prompt`, `spinner_fail`
- Scenario `color_only` detected (missed_rate = 0%)
- Cost ratio ≤1.05x
- Missed rate ≤ baseline on all scenarios

If gate passes: commit the reports, flip `CLIVE_STREAMING_OBS=1` to default-on in `session.py`. If gate fails: root-cause before moving to Phase 2.

```bash
git add evals/observation/phase1-report.{md,json}
git commit -m "docs(observation): phase 1 measurements + gate check"
# If gate passed and default flipped:
git commit -am "feat(observation): CLIVE_STREAMING_OBS default on (phase 1 passed gate)"
```

**Phase 1 complete.**

---

## Phase 2 — Speculation scheduler

**Rationale:** L2 gives us events sooner; speculation uses that head-start to overlap LLM inference with pane settling.

### Task 2.1: `SpeculationScheduler` — unit-level fire/accept

**Files:**
- Create: `src/clive/execution/speculative.py`
- Test: `tests/test_speculative_scheduler.py`

**Step 1: Write the failing test**

```python
# tests/test_speculative_scheduler.py
"""Unit tests for SpeculationScheduler — LLM client is mocked."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from byte_classifier import ByteEvent
from speculative import SpeculationScheduler, SPEC_TRIGGERS


def _evt(kind="cmd_end"):
    return ByteEvent(kind=kind, match_bytes=b"X", stream_offset=0, timestamp=0.0)


def test_fire_records_in_flight():
    client = MagicMock()
    sched = SpeculationScheduler(client, model="test-model")
    sched.fire(_evt(), messages_snapshot=[{"role": "user", "content": "hi"}])
    assert len(sched.in_flight) == 1


def test_fire_respects_max_in_flight():
    client = MagicMock()
    sched = SpeculationScheduler(client, model="test-model")
    for _ in range(5):
        sched.fire(_evt(), messages_snapshot=[])
    assert len(sched.in_flight) <= sched.MAX_IN_FLIGHT


def test_rate_limit_drops_rapid_fires():
    import time
    client = MagicMock()
    sched = SpeculationScheduler(client, model="test-model")
    sched.fire(_evt(), messages_snapshot=[])
    fired_again = sched.fire(_evt(), messages_snapshot=[])
    # Second fire within MIN_FIRE_INTERVAL should be dropped
    assert fired_again is False
    time.sleep(sched.MIN_FIRE_INTERVAL + 0.05)
    fired_again = sched.fire(_evt(), messages_snapshot=[])
    assert fired_again is True


def test_try_consume_rejects_stale_version():
    client = MagicMock()
    sched = SpeculationScheduler(client, model="test-model")
    # Force accepted_version ahead
    sched.accepted_version = 5
    # Fake an in-flight call with lower version
    from speculative import SpecCall
    sched.in_flight = [SpecCall(
        version=3, trigger=_evt(), future=MagicMock(),
        messages_snapshot=[], started_at=0.0,
    )]
    result = sched.try_consume(current_messages=[])
    assert result == (None, 0, 0)


def test_try_consume_rejects_on_messages_prefix_mismatch():
    client = MagicMock()
    sched = SpeculationScheduler(client, model="test-model")
    snap = [{"role": "user", "content": "old"}]
    from speculative import SpecCall
    future = MagicMock()
    future.done.return_value = True
    future.result.return_value = ("reply", 100, 50)
    sched.in_flight = [SpecCall(
        version=1, trigger=_evt(), future=future,
        messages_snapshot=snap, started_at=0.0,
    )]
    # Different prefix — should reject
    result = sched.try_consume(current_messages=[{"role": "user", "content": "new"}])
    assert result == (None, 0, 0)


def test_circuit_breaker_disables_after_threshold():
    client = MagicMock()
    sched = SpeculationScheduler(client, model="test-model")
    for _ in range(sched.BREAKER_THRESHOLD + 1):
        sched._record_cancel()
    sched.fire(_evt(), messages_snapshot=[])
    assert len(sched.in_flight) == 0
```

**Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_speculative_scheduler.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implementation**

```python
# src/clive/execution/speculative.py
"""Speculative LLM call scheduler.

Fires chat_stream() calls on high-confidence L2 triggers, running in
parallel with pane settling. Version-stamped; newer results supersede
older. Bounded concurrency + rate limit + circuit breaker bound cost.

Runs on the pane's asyncio loop (PaneLoop); submit() returns a
concurrent.futures.Future for the sync runner to try_consume().
"""
import collections
import logging
import time
from dataclasses import dataclass
from typing import Any

from byte_classifier import ByteEvent

log = logging.getLogger(__name__)

SPEC_TRIGGERS = {
    "cmd_end", "password_prompt", "confirm_prompt",
    "error_keyword", "permission_error",
}


@dataclass
class SpecCall:
    version: int
    trigger: ByteEvent
    future: Any                          # concurrent.futures.Future
    messages_snapshot: list[dict]
    started_at: float


class SpeculationScheduler:
    MAX_IN_FLIGHT = 2
    MIN_FIRE_INTERVAL = 0.2
    BREAKER_THRESHOLD = 5
    BREAKER_WINDOW = 60.0

    def __init__(self, client, model: str, pane_loop=None):
        self.client = client
        self.model = model
        self.pane_loop = pane_loop
        self.in_flight: list[SpecCall] = []
        self.latest_version = 0
        self.accepted_version = 0
        self._last_fire_ts = 0.0
        self._cancel_times: collections.deque = collections.deque(maxlen=20)

    def fire(self, trigger: ByteEvent, messages_snapshot: list[dict]) -> bool:
        if self._breaker_tripped():
            return False
        now = time.monotonic()
        if now - self._last_fire_ts < self.MIN_FIRE_INTERVAL:
            return False
        self._last_fire_ts = now

        if len(self.in_flight) >= self.MAX_IN_FLIGHT:
            oldest = min(self.in_flight, key=lambda c: c.version)
            oldest.future.cancel()
            self.in_flight.remove(oldest)
            self._record_cancel()

        self.latest_version += 1
        v = self.latest_version
        future = self._submit_call(v, messages_snapshot)
        self.in_flight.append(SpecCall(
            version=v, trigger=trigger, future=future,
            messages_snapshot=list(messages_snapshot),
            started_at=now,
        ))
        return True

    def try_consume(self, current_messages: list[dict]) -> tuple[str | None, int, int]:
        """Return (reply, prompt_tokens, completion_tokens) if an accepted
        call is ready, else (None, 0, 0). Cleans up stale entries."""
        # Prefer newest completed call whose snapshot matches current prefix
        for call in sorted(self.in_flight, key=lambda c: -c.version):
            if call.version <= self.accepted_version:
                continue
            if not call.future.done():
                continue
            if not self._snapshot_matches(call.messages_snapshot, current_messages):
                self.in_flight.remove(call)
                continue
            try:
                reply, pt, ct = call.future.result()
            except Exception:
                self.in_flight.remove(call)
                continue
            self.accepted_version = call.version
            self._cancel_older_than(call.version)
            return reply, pt, ct
        return None, 0, 0

    def _snapshot_matches(self, snap, current) -> bool:
        if len(snap) > len(current):
            return False
        return current[: len(snap)] == snap

    def _cancel_older_than(self, v: int):
        keep = []
        for call in self.in_flight:
            if call.version <= v:
                call.future.cancel()
            else:
                keep.append(call)
        self.in_flight = keep

    def _record_cancel(self):
        self._cancel_times.append(time.monotonic())

    def _breaker_tripped(self) -> bool:
        now = time.monotonic()
        recent = [t for t in self._cancel_times if now - t <= self.BREAKER_WINDOW]
        return len(recent) > self.BREAKER_THRESHOLD

    def _submit_call(self, version: int, messages_snapshot: list[dict]):
        # Concrete LLM invocation — use chat() via the pane loop.
        # For unit tests, pane_loop is None and we use a sync MagicMock.
        from concurrent.futures import Future
        if self.pane_loop is None:
            f = Future()
            f.set_result(("speculative-reply", 0, 0))
            return f
        from llm import chat
        return self.pane_loop.submit(_run_call(self.client, self.model, messages_snapshot))


async def _run_call(client, model, messages):
    from llm import chat_stream
    return chat_stream(client, messages, model=model)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_speculative_scheduler.py -v`
Expected: 6 tests pass.

**Step 5: Commit**

```bash
git add src/clive/execution/speculative.py tests/test_speculative_scheduler.py
git commit -m "feat(execution): SpeculationScheduler — version-stamped LLM calls

Fires chat_stream on L2 high-confidence triggers. MAX_IN_FLIGHT=2, 200ms
rate limit, 5-cancel-per-60s circuit breaker. try_consume checks version
monotonicity + snapshot prefix match to guarantee ordering."
```

---

### Task 2.2: Runner integration — spec_watch + try_consume

**Files:**
- Modify: `src/clive/execution/interactive_runner.py:92-292` — attach scheduler, start `_spec_watch`, call `try_consume` in the turn loop
- Test: `tests/test_interactive_runner_speculation.py`

**Step 1: Write the failing test**

```python
# tests/test_interactive_runner_speculation.py
"""Integration: accepted spec result short-circuits chat_stream in the turn loop."""
from unittest.mock import MagicMock, patch

from interactive_runner import run_subtask_interactive
from models import Subtask, PaneInfo, PaneStream


def test_accepted_spec_result_skips_chat_stream(monkeypatch):
    # Construct a scenario where scheduler.try_consume returns a ready reply
    # and assert chat_stream is NOT invoked this turn.
    ...


def test_mismatched_snapshot_falls_back_to_chat_stream(monkeypatch):
    # When scheduler has no matching result, chat_stream IS invoked.
    ...
```

(Test scaffolding depends on existing runner test patterns — see `tests/test_interactive_runner.py` if present, or adapt from `tests/test_executor.py`.)

**Step 2: Run to verify it fails**

Expected: `try_consume` attribute/path not wired; reply comes from `chat_stream` always.

**Step 3: Implementation**

In `run_subtask_interactive` (near line 134, after `obs_classifier = ScreenClassifier()`):

```python
from speculative import SpeculationScheduler, SPEC_TRIGGERS

scheduler = None
spec_watch_future = None
if pane_info.stream and pane_info.pane_loop:
    scheduler = SpeculationScheduler(client, effective_model, pane_loop=pane_info.pane_loop)
    async def _spec_watch():
        q = pane_info.stream.subscribe()
        while True:
            evt = await q.get()
            if evt.kind in SPEC_TRIGGERS:
                scheduler.fire(evt, messages_snapshot=list(messages))
    spec_watch_future = pane_info.pane_loop.submit(_spec_watch())
```

In the turn loop (around line 184, before `chat_stream`):

```python
reply = pt = ct = None
if scheduler is not None:
    reply, pt, ct = scheduler.try_consume(current_messages=messages)
if reply is None:
    detector = EarlyDoneDetector()
    try:
        reply, pt, ct = chat_stream(client, messages, model=effective_model,
                                    on_token=detector.feed,
                                    should_stop=detector.should_stop)
    except Exception:
        # ...existing fallback...
```

Teardown at end of runner:

```python
if spec_watch_future is not None:
    spec_watch_future.cancel()
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_interactive_runner_speculation.py tests/ --tb=no -q`
Expected: new tests pass, existing 894+ unaffected.

**Step 5: Commit**

```bash
git add src/clive/execution/interactive_runner.py tests/test_interactive_runner_speculation.py
git commit -m "feat(execution): runner consumes speculative LLM results

When pane has a stream + pane_loop, spawns spec_watch task that fires
scheduler on L2 triggers. Turn loop calls try_consume before chat_stream;
falls back cleanly on mismatch."
```

---

### Task 2.3: `latency_bench.py` — phase2 mode + final gate

**Files:**
- Modify: `evals/observation/latency_bench.py` — add `run_scenario_phase2`
- Test: `tests/test_latency_bench.py` — phase2 assertions

**Step 1: Write the failing test**

```python
@pytest.mark.slow
def test_phase2_beats_phase1_on_intervention():
    from evals.observation.scenarios import SCENARIOS
    from evals.observation.latency_bench import run_scenario_phase1, run_scenario_phase2
    scenario = next(s for s in SCENARIOS if s.id == "password_prompt")
    p1 = [run_scenario_phase1(scenario).e2e_latency_ms for _ in range(5)]
    p2 = [run_scenario_phase2(scenario).e2e_latency_ms for _ in range(5)]
    # Expect phase2 median to be faster than phase1 median (speculation win)
    from statistics import median
    assert median(p2) <= median(p1)


@pytest.mark.slow
def test_phase2_cost_within_1_8x_baseline():
    from evals.observation.scenarios import SCENARIOS
    from evals.observation.latency_bench import run_scenario_baseline, run_scenario_phase2
    scenario = next(s for s in SCENARIOS if s.id == "password_prompt")
    base_cost = sum(run_scenario_baseline(scenario).cost_tokens for _ in range(5))
    p2_cost = sum(run_scenario_phase2(scenario).cost_tokens for _ in range(5))
    assert p2_cost <= base_cost * 1.8
```

**Step 2: Run to verify fails**

Expected: `ImportError: cannot import name 'run_scenario_phase2'`.

**Step 3: Implementation**

Add `run_scenario_phase2` to `latency_bench.py`. Mirrors `run_scenario_phase1` but also instantiates the `SpeculationScheduler` and measures spec_waste.

**Step 4: Run tests + full bench**

```bash
python3 -m pytest tests/test_latency_bench.py -v -m slow
python3 evals/observation/latency_bench.py --mode phase2 --runs 50 \
  --out evals/observation/phase2-report.json > evals/observation/phase2-report.md
```

Phase 2 gate (see design §8.3):
- Median `e2e_latency_ms` reduction ≥50% on scenarios 1-3, 5 vs. baseline
- Cost ratio ≤1.8x
- Missed rate ≤ baseline
- `spec_waste` <70%

**Step 5: Commit**

```bash
git add evals/observation/latency_bench.py tests/test_latency_bench.py \
        evals/observation/phase2-report.{md,json}
git commit -m "feat(observation): phase2 bench mode + measurements

Gate decision: PASS/FAIL — see phase2-report.md for the numbers."
```

If gate passes: flip scheduler default-on; if not: keep scheduler behind a sub-flag (`CLIVE_SPECULATE=1`) so Phase 1 can ship without it.

---

## Phase completion

After Phase 2 gate resolves:

1. Update `CLAUDE.md` in the worktree to document the new `CLIVE_STREAMING_OBS` (and if applicable `CLIVE_SPECULATE`) flags. Mention FIFO lifecycle under "Conventions & gotchas."
2. Run `superpowers:requesting-code-review` before merging.
3. Run `superpowers:finishing-a-development-branch` to merge/PR/cleanup the worktree.

## Rollback

Each phase is independently revertible:
- **Phase 2:** revert the `speculative.py` module + the two runner integration commits (Task 2.1-2.2). Phase 1 stays.
- **Phase 1:** set `CLIVE_STREAMING_OBS=0` (or flip the default back). Pane lifecycle skips stream creation; runner takes the poll path. Bit-identical to today.
- **Phase 0:** artifact-only, no runtime code affected.
