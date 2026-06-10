"""Data classes for the self-learning tool discovery system (gh#41)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProbeOutcome:
    """One command tried during exploration."""
    command: str
    exit_code: int | None
    screen: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass
class ExplorationResult:
    """Aggregated result of an exploration session."""
    tool_name: str
    probes: list[ProbeOutcome] = field(default_factory=list)
    summary: str = ""

    @property
    def success_count(self) -> int:
        return sum(1 for p in self.probes if p.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for p in self.probes if not p.success)


@dataclass
class RefinementSignal:
    """One eval outcome feeding driver refinement (gh#41 Phase 3).

    Mirrors the fields of ``evals.harness.metrics.ToolEvalResult`` without
    importing it — ``src/clive/`` must not depend on the eval harness, so
    the orchestrator converts via :meth:`from_eval_result` (duck-typed:
    any object carrying these attributes works).
    """
    task_id: str
    passed: bool
    detail: str = ""
    tool_expected: str | None = None
    tool_used: str | None = None
    tool_correct: bool = True
    flags_correct: bool = True
    fallback_used: bool = False
    fallback_expected: bool = False
    discovery_turns: int = 0

    @classmethod
    def from_eval_result(cls, r) -> "RefinementSignal":
        return cls(
            task_id=r.task_id,
            passed=r.passed,
            detail=r.detail,
            tool_expected=getattr(r, "tool_expected", None),
            tool_used=getattr(r, "tool_used", None),
            tool_correct=getattr(r, "tool_correct", True),
            flags_correct=getattr(r, "flags_correct", True),
            fallback_used=getattr(r, "fallback_used", False),
            fallback_expected=getattr(r, "fallback_expected", False),
            discovery_turns=getattr(r, "discovery_turns", 0),
        )

    @property
    def is_failure(self) -> bool:
        """True when this outcome should drive refinement: an outright
        fail, a wrong tool/flags pick, or an UNEXPECTED fallback (the
        driver steered the agent away from the expected tool). A fallback
        in a fallback eval (tool deliberately disabled) is correct
        behavior, not a signal."""
        return (
            not self.passed
            or not self.tool_correct
            or not self.flags_correct
            or (self.fallback_used and not self.fallback_expected)
        )
