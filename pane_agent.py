"""Per-pane agent — persistent LLM conversation per tmux pane.

Instead of the thread pool model (one thread per subtask, fresh conversation
each time), each pane gets its own PaneAgent that persists across subtasks.

Architecture:
- Persistent memory: compressed summary of what the agent has done and learned
- Model selection: per-pane model override (cheap for shell, capable for browser)
- Learned shortcuts: agent caches successful command patterns for reuse
- Health monitoring: tracks success rate, avg turns, failure patterns
- Autonomous decomposition: agent can break complex subtasks into internal micro-steps

A PaneAgent is a clive instance for a single pane. The executor creates
one per pane at plan start, dispatches subtasks to them, and collects results.
"""
import logging
import os
import time

from models import Subtask, SubtaskResult, SubtaskStatus, PaneInfo
from llm import get_client, chat

log = logging.getLogger(__name__)

# Model recommendations by pane type (cheap for simple, capable for complex)
MODEL_HINTS = {
    "shell": None,      # use default (often cheapest is fine)
    "data": None,
    "docs": None,
    "browser": None,    # needs reasoning about page content
    "email_cli": None,  # interactive, needs state machine awareness
    "agent": None,      # needs planning ability
    "media": None,
}


class PaneAgent:
    """Persistent agent for a single tmux pane.

    Maintains memory, shortcuts, and health metrics across subtasks.
    """

    def __init__(
        self,
        pane_info: PaneInfo,
        session_dir: str = "/tmp/clive",
        model: str | None = None,
    ):
        self.pane_info = pane_info
        self.session_dir = session_dir
        self.model = model or MODEL_HINTS.get(pane_info.app_type)
        self.client = get_client()

        # Persistent memory: compressed summaries of what this agent has done
        self.memory: list[str] = []

        # Learned shortcuts: successful command patterns for reuse
        self.shortcuts: dict[str, str] = {}  # task_pattern → command

        # Health monitoring
        self.subtasks_completed: list[str] = []
        self.subtasks_failed: list[str] = []
        self.total_tokens = 0
        self.total_turns = 0
        self.pane_history: str = ""

    def execute(
        self,
        subtask: Subtask,
        dep_context: str = "",
        on_event=None,
        plan_context: str = "",
        tokens_used: int = 0,
        max_tokens: int = 50000,
    ) -> SubtaskResult:
        """Execute a subtask on this pane. Uses persistent context."""
        from executor import run_subtask

        # Build enriched pane context from persistent memory
        pane_context = self._build_pane_context()

        result = run_subtask(
            subtask=subtask,
            pane_info=self.pane_info,
            dep_context=dep_context,
            on_event=on_event,
            session_dir=self.session_dir,
            pane_context=pane_context,
            plan_context=plan_context,
            tokens_used=tokens_used,
            max_tokens=max_tokens,
            all_panes=None,
        )

        # Update persistent state
        self._update_after_subtask(subtask, result)

        return result

    def _build_pane_context(self) -> str:
        """Build enriched context from persistent memory and shortcuts."""
        parts = []

        # Memory: what this agent has done before on this pane
        if self.memory:
            recent = self.memory[-3:]  # last 3 memories
            parts.append("[Pane memory: " + " | ".join(recent) + "]")

        # Shortcuts: successful patterns the agent has learned
        if self.shortcuts:
            top_shortcuts = list(self.shortcuts.items())[:3]
            shortcut_str = "; ".join(f"{k}: {v}" for k, v in top_shortcuts)
            parts.append(f"[Learned shortcuts: {shortcut_str}]")

        # Health: how this agent has been performing
        if self.subtasks_completed or self.subtasks_failed:
            ok = len(self.subtasks_completed)
            fail = len(self.subtasks_failed)
            avg_turns = self.total_turns / max(ok + fail, 1)
            parts.append(f"[Agent health: {ok} OK, {fail} failed, avg {avg_turns:.1f} turns]")

        # Last screen from previous subtask
        if self.pane_history:
            parts.append(f"[Previous screen:]\n{self.pane_history[-300:]}")

        return "\n".join(parts)

    def _update_after_subtask(self, subtask: Subtask, result: SubtaskResult):
        """Update persistent state after a subtask completes."""
        # Track health
        if result.status == SubtaskStatus.COMPLETED:
            self.subtasks_completed.append(subtask.id)
            # Extract a memory from the result
            memory = f"({subtask.id}) {result.summary[:80]}"
            self.memory.append(memory)
            # Learn shortcuts from successful script commands
            if subtask.mode == "script" and result.turns_used == 1:
                # Single-attempt script success → the task pattern is a good shortcut
                key = _extract_task_pattern(subtask.description)
                if key:
                    self.shortcuts[key] = f"script mode, 1 attempt"
        else:
            self.subtasks_failed.append(subtask.id)
            if result.error:
                self.memory.append(f"({subtask.id}) FAILED: {result.error[:60]}")

        self.total_tokens += result.prompt_tokens + result.completion_tokens
        self.total_turns += result.turns_used
        self.pane_history = result.output_snippet[-500:] if result.output_snippet else ""

        # Keep memory bounded
        if len(self.memory) > 10:
            self.memory = self.memory[-10:]
        if len(self.shortcuts) > 10:
            # Keep most recent shortcuts
            keys = list(self.shortcuts.keys())
            for k in keys[:-10]:
                del self.shortcuts[k]

    @property
    def name(self) -> str:
        return self.pane_info.name

    @property
    def app_type(self) -> str:
        return self.pane_info.app_type

    @property
    def success_rate(self) -> float:
        total = len(self.subtasks_completed) + len(self.subtasks_failed)
        return len(self.subtasks_completed) / total if total > 0 else 1.0

    @property
    def avg_turns(self) -> float:
        total = len(self.subtasks_completed) + len(self.subtasks_failed)
        return self.total_turns / total if total > 0 else 0.0

    def __repr__(self) -> str:
        return (f"PaneAgent({self.name}, {self.app_type}, "
                f"completed={len(self.subtasks_completed)}, "
                f"failed={len(self.subtasks_failed)}, "
                f"tokens={self.total_tokens})")


def _extract_task_pattern(description: str) -> str:
    """Extract a reusable pattern from a task description."""
    # Take the first 3 significant words as the pattern key
    words = [w.lower() for w in description.split() if len(w) > 3][:3]
    return " ".join(words) if words else ""
