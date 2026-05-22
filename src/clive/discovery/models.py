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
