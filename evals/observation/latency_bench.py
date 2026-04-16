"""Latency benchmark driver for observation layer.

Each run:
  1. Spawn a fresh tmux session with a single shell pane.
  2. Instrument pane output via ``pipe-pane`` to a secondary "oracle"
     FIFO that records ground-truth timing independently of the code
     under test. Baseline mode does not consume this yet — Phase 1/2
     will compare detection timing against the oracle bytes.
  3. Run the scenario's shell_command in the pane.
  4. Run the code under test (baseline = today's capture-pane poll loop).
  5. Compute e2e_latency_ms / missed.

Phase 1 and Phase 2 modes land in Tasks 1.7 and 2.3 respectively.

NOTE on FIFO back-pressure: ``tmux pipe-pane`` writes to the FIFO; if
nothing drains it the kernel pipe buffer (~64KB on macOS/Linux) fills up
and blocks the pane. Every scenario here produces well under 64KB so we
intentionally leave the oracle FIFO undrained in baseline mode — the
FIFO is created so Phase 1/2 can attach a reader without re-plumbing.
If future scenarios grow larger, add a background drain thread (or use
a regular file) before you touch this invariant.
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
from completion import wrap_command


# Text patterns per expected L2 kind — mirrors today's wait_for_ready /
# INTERVENTION_PATTERNS regex set, but collapsed to plain ``in`` substring
# checks (that's what a naive poll loop would use).
_TEXT_TARGETS: dict[str, list[str]] = {
    "error_keyword": ["ERROR", "Traceback", "FATAL", "panic:"],
    "password_prompt": ["password:", "Password:"],
    "confirm_prompt": ["[y/N]", "[Y/n]"],
    # cmd_end: prompt sentinels across common shells. macOS default bash
    # (brew) uses "bash-5.3$ "; Linux default often uses "$ " plain;
    # root is "# ". "$ " alone is too permissive (matches env vars in
    # output); "\n$ " and "\n# " require the prompt to be at start of a
    # fresh line, which is what we actually care about.
    "cmd_end": ["\n$ ", "\n# ", "bash-"],
    # SGR-only / blink-only events: baseline cannot detect these through
    # capture-pane -p (which strips SGR by default). Leave empty so the
    # poll loop times out and marks the run as missed.
    "color_alert": [],
    "color_bg_alert": [],
    "blink_attr": [],
}


def _oracle_fifo_path(run_id: str) -> str:
    """Create a fresh oracle FIFO under /tmp/clive-bench/ and return its path."""
    p = f"/tmp/clive-bench/{run_id}-oracle.fifo"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        os.unlink(p)
    # Owner-only — pane bytes may carry sensitive content. See F-1 in
    # security/260416-2100-streaming-observation-audit/findings.md.
    os.mkfifo(p, mode=0o600)
    return p


def run_scenario_baseline(scenario: Scenario, timeout: float = 10.0) -> RunResult:
    """Run a scenario against today's poll-based loop, measure latency.

    Invariants:
      * mode = "baseline"
      * detect_latency_ms = None (baseline has no L2 stage)
      * cost_tokens = 0 (no LLM calls)
      * For baseline_blind scenarios, missed = True.
      * For non-blind scenarios, missed = False when the target text lands
        within the timeout; True otherwise.
      * Session is always killed in ``finally``.
    """
    run_id = uuid.uuid4().hex
    session = f"bench-{run_id}"
    oracle = _oracle_fifo_path(run_id)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "bash"],
        check=True,
    )
    try:
        # Instrument ground truth independently of the code under test.
        # Baseline does not read the FIFO — see module docstring.
        subprocess.run(
            [
                "tmux", "pipe-pane", "-t", f"{session}:0.0",
                f"cat > {oracle}",
            ],
            check=True,
        )

        # Wait for the shell to render its first prompt before send-keys
        # fires. Without this, pipe-pane (step above) and send-keys (step
        # below) race: send-keys can reach the pane before the shell has
        # written its prompt, so the initial snapshot taken inside
        # _poll_for_baseline ends up containing the command echo rather
        # than the prompt, and subsequent prompt renderings aren't
        # counted as new. (Distinct from the initial-count defense in
        # _poll_for_baseline, which handles prompts that *were* on
        # screen pre-command.)
        time.sleep(0.1)

        # Wrap the command the same way production does (interactive_runner
        # always calls wrap_command before send-keys). This makes baseline
        # and phase1 measurements apples-to-apples: both see the same
        # EXIT:<n> ___DONE_... completion marker on the pane.
        wrapped, marker = wrap_command(
            scenario.shell_command, subtask_id=f"bench-{run_id[:8]}",
        )

        t0 = time.monotonic()
        subprocess.run(
            [
                "tmux", "send-keys", "-t", f"{session}:0.0",
                wrapped, "Enter",
            ],
            check=True,
        )

        t_detect, missed = _poll_for_baseline(
            session, scenario, start=t0, timeout=timeout, marker=marker,
        )
        # When missed, e2e is reported as 0.0 per RunResult contract
        # (see metrics.py — aggregate() filters missed runs from latency
        # stats via the `missed` flag).
        e2e_ms = (t_detect - t0) * 1000 if t_detect else 0.0

        return RunResult(
            scenario_id=scenario.id,
            mode="baseline",
            detect_latency_ms=None,
            e2e_latency_ms=e2e_ms,
            missed=missed,
            cost_tokens=0,
        )
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )
        if os.path.exists(oracle):
            try:
                os.unlink(oracle)
            except OSError:
                pass


def run_scenario_phase1(scenario: Scenario, timeout: float = 10.0) -> RunResult:
    """Run a scenario against the Phase 1 FIFO+ByteClassifier pipeline.

    Invariants:
      * mode = "phase1"
      * detect_latency_ms populated (Phase 1 has the L2 stage)
      * cost_tokens = 0 (no LLM calls)
      * For Phase 1, detect_latency_ms == e2e_latency_ms (no LLM
        involved; detection IS the e2e for bench purposes).
      * For baseline_blind scenarios (color_only): phase1 MUST detect —
        that's the load-bearing demo that L2 sees pure-SGR signals the
        poll path cannot.
      * Session is always killed and FIFO unlinked in ``finally``.
    """
    import asyncio
    from fifo_stream import PaneStream
    from byte_classifier import ByteEvent  # noqa: F401  (type only)

    run_id = uuid.uuid4().hex
    session = f"bench-p1-{run_id}"
    fifo_path = f"/tmp/clive-bench/{run_id}-p1.fifo"
    os.makedirs(os.path.dirname(fifo_path), exist_ok=True)
    if os.path.exists(fifo_path):
        os.unlink(fifo_path)
    # Owner-only FIFO — see F-1 in the streaming-observation audit.
    os.mkfifo(fifo_path, mode=0o600)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "bash"],
        check=True,
    )
    try:
        # Let the shell write its initial prompt before we start piping
        # — same reasoning as run_scenario_baseline.
        time.sleep(0.1)
        # pipe-pane all future output to the FIFO that PaneStream reads.
        subprocess.run(
            [
                "tmux", "pipe-pane", "-t", f"{session}:0.0",
                f"cat > {fifo_path}",
            ],
            check=True,
        )

        # Wrap the command the same way production does. The ByteClassifier
        # cmd_end pattern matches EXIT:\d+ ___DONE_ — the digit guard rejects
        # the "EXIT:$?" literal in the send-keys echo, so only the real
        # completion fires the event.
        wrapped, _marker = wrap_command(
            scenario.shell_command, subtask_id=f"bench-p1-{run_id[:8]}",
        )

        async def _run() -> tuple[float | None, bool]:
            stream = PaneStream.from_fifo_path(fifo_path)
            q = stream.subscribe()
            try:
                t0 = time.monotonic()
                subprocess.run(
                    [
                        "tmux", "send-keys", "-t", f"{session}:0.0",
                        wrapped, "Enter",
                    ],
                    check=True,
                )
                deadline = t0 + timeout
                targets = set(scenario.expected_l2_kinds)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None, True
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        return None, True
                    if evt.kind in targets:
                        return time.monotonic() - t0, False
                    # Event didn't match — keep draining until a target
                    # lands or we hit the deadline.
            finally:
                await stream.close()

        latency, missed = asyncio.run(_run())
        latency_ms = latency * 1000 if latency is not None else 0.0
        return RunResult(
            scenario_id=scenario.id,
            mode="phase1",
            detect_latency_ms=latency_ms if not missed else None,
            e2e_latency_ms=latency_ms,
            missed=missed,
            cost_tokens=0,
        )
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )
        if os.path.exists(fifo_path):
            try:
                os.unlink(fifo_path)
            except OSError:
                pass


def _poll_for_baseline(
    session: str,
    scenario: Scenario,
    start: float,
    timeout: float,
    marker: str | None = None,
) -> tuple[float | None, bool]:
    """Mimic today's wait_for_ready: capture-pane at adaptive 10→500ms backoff.

    Returns (t_detect, missed). When no target fires before ``timeout``,
    returns (None, True).

    When ``marker`` is provided and cmd_end is an expected kind, the marker
    substring is added to the cmd_end targets — matches production's
    completion detection via wrap_command.
    """
    poll_interval = 0.010
    deadline = start + timeout

    targets: list[str] = []
    for kind in scenario.expected_l2_kinds:
        targets.extend(_TEXT_TARGETS.get(kind, []))
    if marker and "cmd_end" in scenario.expected_l2_kinds:
        targets.append(marker)

    # Snapshot the starting screen so cmd_end / prompt-style patterns
    # that were already on screen pre-command don't count as detection.
    initial = subprocess.run(
        ["tmux", "capture-pane", "-t", f"{session}:0.0", "-p"],
        capture_output=True, text=True, check=False,
    ).stdout

    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", f"{session}:0.0", "-p"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            # Session died mid-poll; bail out.
            return None, True
        out = result.stdout

        for t in targets:
            if t and t in out and out.count(t) > initial.count(t):
                return time.monotonic(), False

        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 0.5)

    return None, True


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode",
        choices=["baseline", "phase1", "phase2"],
        required=True,
    )
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--out", default="evals/observation/report.json")
    ap.add_argument(
        "--timeout", type=float, default=10.0,
        help="Per-run scenario timeout in seconds (default: 10.0)",
    )
    args = ap.parse_args(argv)

    if not shutil.which("tmux"):
        raise RuntimeError("tmux is required for latency_bench")

    results: list[RunResult] = []
    for scenario in SCENARIOS:
        for i in range(args.runs):
            if args.mode == "baseline":
                results.append(run_scenario_baseline(scenario, timeout=args.timeout))
            elif args.mode == "phase1":
                results.append(run_scenario_phase1(scenario, timeout=args.timeout))
            else:
                raise NotImplementedError(
                    f"mode={args.mode} lands in a later task"
                )
            print(f"  {scenario.id} run {i+1}/{args.runs}", file=sys.stderr)

    by_scenario: dict[str, list[RunResult]] = {}
    for r in results:
        by_scenario.setdefault(r.scenario_id, []).append(r)
    rows = {
        args.mode: {sid: aggregate(runs) for sid, runs in by_scenario.items()}
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(
            {"mode": args.mode, "runs": [r.__dict__ for r in results]},
            f,
            indent=2,
        )
    print(format_markdown_report(rows))


if __name__ == "__main__":
    main()
