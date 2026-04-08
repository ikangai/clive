"""Per-pane agent — intelligent persistent sub-agent for each tmux pane.

Architecture:
- Persistent memory: compressed summaries, carried across subtasks
- Shared memory: common knowledge shared across all agents via SharedBrain
- Learned shortcuts: successful patterns cached for reuse
- Health monitoring: success rate, turns, failure patterns
- Self-adaptation: degrades gracefully (escalate mode, increase turns)
- Direct messaging: agents can send messages to each other via SharedBrain
- Delegation: agents can request work from other agents
- Session persistence: memory saved to disk for cross-run continuity
"""
import json
import logging
import os
import threading
import time

from models import Subtask, SubtaskResult, SubtaskStatus, PaneInfo
from llm import get_client

log = logging.getLogger(__name__)


# ─── Shared Brain (cross-agent communication + shared memory) ────────────────

class SharedBrain:
    """Shared memory and messaging between all PaneAgents in a session.

    Agents can:
    - Post facts (shared knowledge, available to all agents)
    - Send messages to specific agents
    - Read all shared facts and their messages

    Thread-safe: all mutations are protected by a lock.
    """

    def __init__(self, session_dir: str):
        self.session_dir = session_dir
        self._lock = threading.Lock()
        self.facts: list[dict] = []  # shared knowledge
        self.messages: dict[str, list[dict]] = {}  # agent_name → messages
        self.delegation_queue: list[dict] = []  # work requests between agents

    def post_fact(self, agent: str, fact: str):
        """Post a fact visible to all agents."""
        with self._lock:
            self.facts.append({"agent": agent, "fact": fact, "time": time.time()})
        # Also write to scratchpad for backward compatibility
        scratchpad = os.path.join(self.session_dir, "_scratchpad.jsonl")
        try:
            with open(scratchpad, "a") as f:
                f.write(json.dumps({"agent": agent, "type": "fact", "note": fact}) + "\n")
        except OSError:
            pass

    def send_message(self, from_agent: str, to_agent: str, message: str):
        """Send a direct message to another agent."""
        with self._lock:
            if to_agent not in self.messages:
                self.messages[to_agent] = []
            self.messages[to_agent].append({
                "from": from_agent, "message": message, "time": time.time()
            })

    def get_messages(self, agent_name: str) -> list[dict]:
        """Get and clear pending messages for an agent."""
        with self._lock:
            msgs = self.messages.pop(agent_name, [])
        return msgs

    def request_work(self, from_agent: str, target_pane: str, description: str):
        """Request work from another agent's pane."""
        with self._lock:
            self.delegation_queue.append({
                "from": from_agent, "target_pane": target_pane,
                "description": description, "time": time.time()
            })

    def get_delegated_work(self, pane_name: str) -> list[dict]:
        """Get and clear delegated work requests for a pane."""
        with self._lock:
            mine = [d for d in self.delegation_queue if d["target_pane"] == pane_name]
            self.delegation_queue = [d for d in self.delegation_queue if d["target_pane"] != pane_name]
        return mine

    def get_context_for_agent(self, agent_name: str) -> str:
        """Build shared context string for an agent's turn."""
        parts = []

        # Shared facts (last 5)
        with self._lock:
            recent_facts = list(self.facts[-5:])
        if recent_facts:
            fact_strs = [f"{f['agent']}: {f['fact']}" for f in recent_facts]
            parts.append("[Shared knowledge: " + " | ".join(fact_strs) + "]")

        # Direct messages (get_messages acquires lock internally)
        msgs = self.get_messages(agent_name)
        if msgs:
            msg_strs = [f"{m['from']}: {m['message']}" for m in msgs]
            parts.append("[Messages for you: " + " | ".join(msg_strs) + "]")

        # Delegated work (get_delegated_work acquires lock internally)
        work = self.get_delegated_work(agent_name)
        if work:
            work_strs = [f"from {w['from']}: {w['description']}" for w in work]
            parts.append("[Work requests: " + " | ".join(work_strs) + "]")

        return "\n".join(parts)

    def save(self, path: str):
        """Save shared brain state to disk."""
        data = {"facts": self.facts[-20:]}
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str, session_dir: str) -> "SharedBrain":
        """Load shared brain state from disk."""
        brain = cls(session_dir)
        try:
            with open(path) as f:
                data = json.load(f)
            brain.facts = data.get("facts", [])
        except (OSError, json.JSONDecodeError):
            pass
        return brain


# ─── PaneAgent ────────────────────────────────────────────────────────────────

class PaneAgent:
    """Persistent agent for a single tmux pane."""

    def __init__(
        self,
        pane_info: PaneInfo,
        session_dir: str = "/tmp/clive",
        model: str | None = None,
        shared_brain: SharedBrain | None = None,
    ):
        self.pane_info = pane_info
        self.session_dir = session_dir
        self.model = model
        self.client = get_client()
        self.shared_brain = shared_brain

        # Persistent memory
        self.memory: list[str] = []
        self.shortcuts: dict[str, str] = {}

        # Health monitoring
        self.subtasks_completed: list[str] = []
        self.subtasks_failed: list[str] = []
        self.total_tokens = 0
        self.total_turns = 0
        self.pane_history: str = ""

        # Adaptation state
        self._mode_escalated = False
        self._turns_boosted = False

    def execute(
        self,
        subtask: Subtask,
        dep_context: str = "",
        on_event=None,
        plan_context: str = "",
        tokens_used: int = 0,
        max_tokens: int = 50000,
    ) -> SubtaskResult:
        """Execute a subtask. Uses persistent context + shared brain."""
        from executor import run_subtask

        # Self-adaptation: if failing, escalate
        self._adapt_before_subtask(subtask)

        # Build enriched pane context
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

        self._update_after_subtask(subtask, result)
        return result

    def _adapt_before_subtask(self, subtask: Subtask):
        """Self-adaptation: adjust mode/turns based on health."""
        if len(self.subtasks_failed) >= 2 and not self._mode_escalated:
            if subtask.mode == "script":
                log.info(f"PaneAgent {self.name}: escalating to interactive (2+ failures)")
                subtask.mode = "interactive"
                self._mode_escalated = True

        if self.success_rate < 0.5 and not self._turns_boosted:
            subtask.max_turns = max(subtask.max_turns, 12)
            self._turns_boosted = True
            log.info(f"PaneAgent {self.name}: boosted max_turns to {subtask.max_turns}")

    def _build_pane_context(self) -> str:
        """Build enriched context from memory, shortcuts, health, shared brain."""
        parts = []

        # Memory
        if self.memory:
            recent = self.memory[-3:]
            parts.append("[Pane memory: " + " | ".join(recent) + "]")

        # Shortcuts
        if self.shortcuts:
            top = list(self.shortcuts.items())[:3]
            parts.append(f"[Learned shortcuts: {'; '.join(f'{k}: {v}' for k, v in top)}]")

        # Health
        if self.subtasks_completed or self.subtasks_failed:
            ok = len(self.subtasks_completed)
            fail = len(self.subtasks_failed)
            avg = self.total_turns / max(ok + fail, 1)
            parts.append(f"[Agent health: {ok} OK, {fail} failed, avg {avg:.1f} turns]")

        # Shared brain context
        if self.shared_brain:
            shared_ctx = self.shared_brain.get_context_for_agent(self.name)
            if shared_ctx:
                parts.append(shared_ctx)

        # Previous screen
        if self.pane_history:
            parts.append(f"[Previous screen:]\n{self.pane_history[-300:]}")

        return "\n".join(parts)

    def _update_after_subtask(self, subtask: Subtask, result: SubtaskResult):
        """Update state after subtask completion."""
        if result.status == SubtaskStatus.COMPLETED:
            self.subtasks_completed.append(subtask.id)
            memory = f"({subtask.id}) {result.summary[:80]}"
            self.memory.append(memory)
            # Learn shortcuts from single-attempt script successes
            if subtask.mode == "script" and result.turns_used == 1:
                key = _extract_task_pattern(subtask.description)
                if key:
                    self.shortcuts[key] = "script mode, 1 attempt"
            # Post success fact to shared brain
            if self.shared_brain:
                self.shared_brain.post_fact(self.name, result.summary[:60])
        else:
            self.subtasks_failed.append(subtask.id)
            if result.error:
                self.memory.append(f"({subtask.id}) FAILED: {result.error[:60]}")
            # Post failure fact to shared brain
            if self.shared_brain:
                self.shared_brain.post_fact(self.name, f"FAILED: {result.summary[:40]}")

        self.total_tokens += result.prompt_tokens + result.completion_tokens
        self.total_turns += result.turns_used
        self.pane_history = result.output_snippet[-500:] if result.output_snippet else ""

        # Bound memory
        if len(self.memory) > 10:
            self.memory = self.memory[-10:]
        if len(self.shortcuts) > 10:
            keys = list(self.shortcuts.keys())
            for k in keys[:-10]:
                del self.shortcuts[k]

    # ─── Persistence ──────────────────────────────────────────────────────

    def save(self, path: str):
        """Save agent state to disk for cross-run persistence."""
        data = {
            "pane": self.name,
            "app_type": self.app_type,
            "memory": self.memory,
            "shortcuts": self.shortcuts,
            "completed": len(self.subtasks_completed),
            "failed": len(self.subtasks_failed),
            "total_tokens": self.total_tokens,
            "total_turns": self.total_turns,
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def load_state(self, path: str):
        """Load previous state from disk."""
        try:
            with open(path) as f:
                data = json.load(f)
            self.memory = data.get("memory", [])
            self.shortcuts = data.get("shortcuts", {})
        except (OSError, json.JSONDecodeError):
            pass

    # ─── Properties ───────────────────────────────────────────────────────

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
    words = [w.lower() for w in description.split() if len(w) > 3][:3]
    return " ".join(words) if words else ""
