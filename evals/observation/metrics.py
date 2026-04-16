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
