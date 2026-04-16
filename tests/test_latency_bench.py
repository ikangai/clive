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
