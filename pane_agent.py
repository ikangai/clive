"""Per-pane agent — persistent LLM conversation per tmux pane.

Instead of the thread pool model (one thread per subtask, fresh conversation
each time), each pane gets its own PaneAgent that persists across subtasks.

Benefits:
- Context carries forward: the agent remembers what it did on this pane
- No pane locks needed: each agent owns its pane exclusively
- Model heterogeneity: different panes can use different LLM models
- Cleaner failure isolation: one agent crash doesn't affect others

A PaneAgent is a clive instance for a single pane. The executor creates
one per pane at plan start, dispatches subtasks to them, and collects results.
"""
import logging
import threading
import time

from models import Subtask, SubtaskResult, SubtaskStatus, PaneInfo
from llm import get_client, chat, chat_stream
from prompts import build_worker_prompt
from session import capture_pane
from screen_diff import compute_screen_diff
from completion import wait_for_ready, wrap_command

log = logging.getLogger(__name__)


class PaneAgent:
    """Persistent agent for a single tmux pane.

    Maintains its own conversation history across subtasks.
    Owns its pane exclusively — no external locking needed.
    """

    def __init__(
        self,
        pane_info: PaneInfo,
        session_dir: str = "/tmp/clive",
        model: str | None = None,
    ):
        self.pane_info = pane_info
        self.session_dir = session_dir
        self.model = model  # per-pane model override
        self.client = get_client()
        self.subtasks_completed: list[str] = []
        self.total_tokens = 0
        self.pane_history: str = ""  # running context of what's been done

    def execute(
        self,
        subtask: Subtask,
        dep_context: str = "",
        on_event=None,
        plan_context: str = "",
        tokens_used: int = 0,
        max_tokens: int = 50000,
    ) -> SubtaskResult:
        """Execute a subtask on this pane. Uses the persistent pane context."""
        from executor import run_subtask

        # Inject pane history from previous subtasks on this pane
        pane_context = self.pane_history

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
            all_panes=None,  # peek handled at executor level
        )

        # Update pane history with what this subtask did
        self.pane_history = result.output_snippet[-500:] if result.output_snippet else ""
        self.subtasks_completed.append(subtask.id)
        self.total_tokens += result.prompt_tokens + result.completion_tokens

        return result

    @property
    def name(self) -> str:
        return self.pane_info.name

    @property
    def app_type(self) -> str:
        return self.pane_info.app_type

    def __repr__(self) -> str:
        return (f"PaneAgent({self.name}, {self.app_type}, "
                f"completed={len(self.subtasks_completed)}, tokens={self.total_tokens})")
