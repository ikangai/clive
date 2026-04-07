"""Fitness scoring for driver prompt evolution.

Composite score from three metrics:
  - pass_rate (weight 0.5): tasks passed / total
  - turn_efficiency (weight 0.3): min_turns / actual_turns, averaged
  - token_efficiency (weight 0.2): inverse of normalized token usage

Pass rate has a hard floor: if pass_rate < baseline, fitness is 0.
"""
from evals.harness.metrics import EvalReport

W_PASS = 0.5
W_TURN = 0.3
W_TOKEN = 0.2
TOKEN_BUDGET_PER_TASK = 10_000


def fitness_score(report: EvalReport, baseline_pass_rate: float = 0.0) -> float:
    if report.total_tasks == 0:
        return 0.0
    pass_rate = report.completion_rate
    if pass_rate < baseline_pass_rate:
        return 0.0
    turn_efficiency = report.avg_turn_efficiency
    avg_tokens = report.total_tokens / report.total_tasks
    token_efficiency = max(0.0, 1.0 - avg_tokens / TOKEN_BUDGET_PER_TASK)
    return (W_PASS * pass_rate) + (W_TURN * turn_efficiency) + (W_TOKEN * token_efficiency)
