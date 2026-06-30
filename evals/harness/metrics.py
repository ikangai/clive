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
class ToolEvalResult(EvalResult):
    """EvalResult extended with tool-discovery metrics (gh#40, Layer 5).

    `passed` requires both the outcome check and the discovery criteria;
    these fields record the process so reports can split "right answer"
    from "right tool via discovery".
    """
    tool_used: str | None = None           # Which tool the agent actually used
    tool_expected: str | None = None       # Which tool was expected
    tool_correct: bool = True              # Did agent pick the right tool?
    discovery_turns: int = 0               # clive-tools invocations observed
    flags_correct: bool = True             # Did agent use correct flags/syntax?
    pipeline_stages: int = 0               # Number of tools chained
    fallback_used: bool = False            # Did agent fall back to alternative?
    fallback_expected: bool = False        # Did the task expect a fallback?


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

    # ---- tool-discovery metrics (gh#40); defined over ToolEvalResults ----

    @property
    def tool_results(self) -> list[ToolEvalResult]:
        return [r for r in self.results if isinstance(r, ToolEvalResult)]

    @property
    def tool_accuracy(self) -> float:
        """% of tool evals where the agent picked the right tool."""
        trs = self.tool_results
        if not trs:
            return 0.0
        return sum(1 for r in trs if r.tool_correct) / len(trs)

    @property
    def flag_accuracy(self) -> float:
        """% of tool evals where the agent invoked the tool with the right flags."""
        trs = self.tool_results
        if not trs:
            return 0.0
        return sum(1 for r in trs if r.flags_correct) / len(trs)

    @property
    def discovery_efficiency(self) -> float:
        """Average clive-tools turns to discover a tool (Layer 5 only).

        Averages over evals that actually used discovery; tasks with
        zero discovery turns are failures of a different kind and would
        skew the efficiency number toward zero.
        """
        l5 = [
            r for r in self.tool_results
            if r.layer == 5 and r.discovery_turns > 0
        ]
        if not l5:
            return 0.0
        return sum(r.discovery_turns for r in l5) / len(l5)

    @property
    def pipeline_success_rate(self) -> float:
        """% of multi-tool pipeline evals (Layer 3) that passed."""
        pipes = [r for r in self.tool_results if r.layer == 3]
        if not pipes:
            return 0.0
        return sum(1 for r in pipes if r.passed) / len(pipes)

    @property
    def fallback_success_rate(self) -> float:
        """% of fallback-expecting evals where the agent actually fell back."""
        expecting = [r for r in self.tool_results if r.fallback_expected]
        if not expecting:
            return 0.0
        return sum(1 for r in expecting if r.fallback_used) / len(expecting)

    def estimated_cost(self, model: str | None = None) -> float:
        """Estimate cost using pricing.json. Returns 0.0 if pricing unavailable."""
        import json as _json
        import os
        pricing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
        try:
            with open(pricing_path) as f:
                pricing = _json.load(f)
            # Look up model-specific rates, fall back to default
            model = model or os.environ.get("AGENT_MODEL", "")
            rates = pricing.get(model, pricing.get("default", {"prompt_per_1k": 0.003, "completion_per_1k": 0.015}))
            total_prompt = sum(r.prompt_tokens for r in self.results)
            total_completion = sum(r.completion_tokens for r in self.results)
            return (total_prompt / 1000 * rates["prompt_per_1k"] +
                    total_completion / 1000 * rates["completion_per_1k"])
        except Exception:
            return 0.0

    def to_dict(self) -> dict:
        d = self._base_dict()
        if self.tool_results:
            d["tool_metrics"] = {
                "tool_accuracy": round(self.tool_accuracy, 3),
                "flag_accuracy": round(self.flag_accuracy, 3),
                "discovery_efficiency": round(self.discovery_efficiency, 3),
                "pipeline_success_rate": round(self.pipeline_success_rate, 3),
                "fallback_success_rate": round(self.fallback_success_rate, 3),
            }
        return d

    def _base_dict(self) -> dict:
        return {
            "total_tasks": self.total_tasks,
            "passed": self.passed_tasks,
            "completion_rate": round(self.completion_rate, 3),
            "avg_turn_efficiency": round(self.avg_turn_efficiency, 3),
            "total_tokens": self.total_tokens,
            "total_elapsed_seconds": round(self.total_elapsed, 1),
            "estimated_cost_usd": round(self.estimated_cost(), 4),
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
        if self.tool_results:
            progress(f"Tool accuracy:   {self.tool_accuracy:.0%}")
            progress(f"Flag accuracy:   {self.flag_accuracy:.0%}")
            if any(r.layer == 5 for r in self.tool_results):
                progress(f"Discovery turns: {self.discovery_efficiency:.1f} avg")
            if any(r.layer == 3 for r in self.tool_results):
                progress(f"Pipeline pass:   {self.pipeline_success_rate:.0%}")
            if any(r.fallback_expected for r in self.tool_results):
                progress(f"Fallback rate:   {self.fallback_success_rate:.0%}")
        progress(f"Total tokens:    {self.total_tokens:,}")
        cost = self.estimated_cost()
        if cost > 0:
            progress(f"Est. cost:       ${cost:.4f}")
        progress(f"Total time:      {self.total_elapsed:.1f}s")
        progress(f"{'=' * 60}\n")
