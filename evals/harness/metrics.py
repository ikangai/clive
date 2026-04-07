"""Eval metrics collection and reporting."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalResult:
    """Result of a single eval task."""
    task_id: str
    layer: int
    tool: str
    passed: bool
    turns_used: int
    min_turns: int
    prompt_tokens: int
    completion_tokens: int
    elapsed_seconds: float
    detail: str
    error_recovered: bool = False
    false_completion: bool = False

    @property
    def turn_efficiency(self) -> float:
        """Ratio of min_turns / turns_used. 1.0 = optimal."""
        if self.turns_used == 0:
            return 0.0
        return self.min_turns / self.turns_used

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class EvalReport:
    """Aggregated eval report."""
    results: list[EvalResult]

    @property
    def total_tasks(self) -> int:
        return len(self.results)

    @property
    def passed_tasks(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def completion_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.passed_tasks / self.total_tasks

    @property
    def avg_turn_efficiency(self) -> float:
        efficiencies = [r.turn_efficiency for r in self.results if r.turns_used > 0]
        if not efficiencies:
            return 0.0
        return sum(efficiencies) / len(efficiencies)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.results)

    @property
    def total_elapsed(self) -> float:
        return sum(r.elapsed_seconds for r in self.results)

    @property
    def error_recovery_rate(self) -> float:
        errored = [r for r in self.results if r.error_recovered or not r.passed]
        if not errored:
            return 1.0
        return sum(1 for r in errored if r.error_recovered) / len(errored)

    @property
    def false_completion_rate(self) -> float:
        completed = [r for r in self.results if r.turns_used > 0]
        if not completed:
            return 0.0
        return sum(1 for r in completed if r.false_completion) / len(completed)

    def estimated_cost(self) -> float:
        """Estimate cost using pricing.json. Returns 0.0 if pricing unavailable."""
        import json as _json
        import os
        pricing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
        try:
            with open(pricing_path) as f:
                pricing = _json.load(f)
            rates = pricing.get("default", {"prompt_per_1k": 0.003, "completion_per_1k": 0.015})
            total_prompt = sum(r.prompt_tokens for r in self.results)
            total_completion = sum(r.completion_tokens for r in self.results)
            return (total_prompt / 1000 * rates["prompt_per_1k"] +
                    total_completion / 1000 * rates["completion_per_1k"])
        except Exception:
            return 0.0

    def to_dict(self) -> dict:
        return {
            "total_tasks": self.total_tasks,
            "passed": self.passed_tasks,
            "completion_rate": round(self.completion_rate, 3),
            "avg_turn_efficiency": round(self.avg_turn_efficiency, 3),
            "total_tokens": self.total_tokens,
            "total_elapsed_seconds": round(self.total_elapsed, 1),
            "error_recovery_rate": round(self.error_recovery_rate, 3),
            "false_completion_rate": round(self.false_completion_rate, 3),
            "results": [
                {
                    "task_id": r.task_id,
                    "passed": r.passed,
                    "turns": r.turns_used,
                    "tokens": r.total_tokens,
                    "elapsed": round(r.elapsed_seconds, 1),
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }

    def print_summary(self):
        """Print a human-readable summary."""
        from output import progress
        progress(f"\n{'=' * 60}")
        progress(f"EVAL RESULTS: {self.passed_tasks}/{self.total_tasks} passed "
                 f"({self.completion_rate:.0%})")
        progress(f"{'=' * 60}")
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            progress(f"  [{status}] {r.task_id} "
                     f"(turns: {r.turns_used}, tokens: {r.total_tokens})")
            if not r.passed:
                progress(f"         {r.detail}")
        progress(f"{'~' * 60}")
        progress(f"Turn efficiency: {self.avg_turn_efficiency:.0%}")
        progress(f"Total tokens:    {self.total_tokens:,}")
        progress(f"Total time:      {self.total_elapsed:.1f}s")
        progress(f"{'=' * 60}\n")
