"""Data structures for the agent planning and execution system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import libtmux


class SubtaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Subtask:
    id: str
    description: str
    pane: str
    depends_on: list[str] = field(default_factory=list)
    status: SubtaskStatus = SubtaskStatus.PENDING
    max_turns: int = 15


@dataclass
class Plan:
    task: str
    subtasks: list[Subtask] = field(default_factory=list)

    def validate(self, valid_panes: set[str]) -> list[str]:
        """Check DAG validity: no cycles, all deps exist, all panes valid."""
        errors = []
        ids = {s.id for s in self.subtasks}

        for s in self.subtasks:
            for dep in s.depends_on:
                if dep not in ids:
                    errors.append(f"Subtask {s.id} depends on unknown subtask {dep}")
            if s.pane not in valid_panes:
                errors.append(
                    f"Subtask {s.id} references unknown pane '{s.pane}'. "
                    f"Available: {valid_panes}"
                )

        # Cycle detection via Kahn's algorithm
        in_degree = {s.id: 0 for s in self.subtasks}
        adj: dict[str, list[str]] = {s.id: [] for s in self.subtasks}
        for s in self.subtasks:
            for dep in s.depends_on:
                if dep in adj:
                    adj[dep].append(s.id)
                    in_degree[s.id] += 1

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            node = queue.pop(0)
            visited += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited < len(self.subtasks):
            errors.append("Cycle detected in subtask dependencies")

        return errors


@dataclass
class SubtaskResult:
    subtask_id: str
    status: SubtaskStatus
    summary: str
    output_snippet: str
    error: Optional[str] = None
    turns_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class PaneInfo:
    pane: libtmux.Pane
    app_type: str
    description: str
    name: str
    idle_timeout: float = 2.0
