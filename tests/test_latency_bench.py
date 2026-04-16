"""Tests for latency_bench driver.

Uses a tmux session on localhost; skipped if tmux not installed.
Baseline mode uses today's wait_for_ready poll path — exercises real
pane + real shell, measures real latency. Tests are slow (~seconds each);
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
    # Baseline has no L2 stage — detect_latency_ms must be None
    assert result.detect_latency_ms is None
    assert result.cost_tokens == 0


@pytest.mark.slow
def test_baseline_color_only_is_missed():
    from evals.observation.scenarios import SCENARIOS
    scenario = next(s for s in SCENARIOS if s.id == "color_only")
    result = run_scenario_baseline(scenario)
    # Baseline fundamentally cannot detect pure SGR changes via capture-pane -p
    assert result.missed is True


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
    assert result.detect_latency_ms >= 0
    assert result.mode == "phase1"


@pytest.mark.slow
def test_phase1_error_scroll_faster_than_baseline_in_best_case():
    """Phase 1 should be at least as fast as baseline on error_scroll in
    the best case. Weak assertion — exact deltas depend on system load."""
    from evals.observation.scenarios import SCENARIOS
    from evals.observation.latency_bench import run_scenario_baseline, run_scenario_phase1
    scenario = next(s for s in SCENARIOS if s.id == "error_scroll")
    base_samples = [run_scenario_baseline(scenario).e2e_latency_ms for _ in range(3)]
    phase1_samples = [run_scenario_phase1(scenario).e2e_latency_ms for _ in range(3)]
    # Weak bound: fastest phase1 run should beat the slowest baseline run.
    # (Strong assertions belong to Task 1.8 gate check.)
    assert min(phase1_samples) < max(base_samples)
