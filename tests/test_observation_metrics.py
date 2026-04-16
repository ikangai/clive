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
    assert agg.median_e2e_ms == 200  # median of [100,200,300] (missed excluded)
    assert agg.missed_rate == pytest.approx(0.25)
    assert agg.n == 4


def test_aggregate_excludes_missed_from_latency_median():
    runs = [_result(100), _result(200), _result(0, missed=True)]
    agg = aggregate(runs)
    assert agg.median_e2e_ms == 150


def test_markdown_report_includes_all_modes():
    rows = {
        "baseline": {"error_scroll": aggregate([_result(500)])},
        "phase1":   {"error_scroll": aggregate([_result(200)])},
    }
    md = format_markdown_report(rows)
    assert "baseline" in md
    assert "phase1" in md
    assert "error_scroll" in md


def test_aggregate_all_missed_returns_none_e2e():
    runs = [_result(0, missed=True), _result(0, missed=True)]
    agg = aggregate(runs)
    assert agg.median_e2e_ms is None
    assert agg.missed_rate == 1.0


def test_markdown_renders_none_e2e_as_dash():
    runs = [_result(0, missed=True)]
    rows = {"baseline": {"error_scroll": aggregate(runs)}}
    md = format_markdown_report(rows)
    # The e2e column for error_scroll must render as "-", not "0"
    line = next(l for l in md.splitlines() if l.startswith("| error_scroll"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    # Cell layout: [scenario, baseline_e2e, baseline_missed%]
    assert cells[1] == "-"
