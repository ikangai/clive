"""Screen observation event system.

Classifies raw tmux screen captures into semantic events using regex
patterns — no LLM calls. The classifier sets a `needs_llm` flag so the
caller knows whether to escalate to the expensive main model.
"""

import re
from dataclasses import dataclass, field
from enum import Enum

from .completion import INTERVENTION_PATTERNS


class EventType(Enum):
    SUCCESS = "success"
    ERROR = "error"
    NEEDS_INPUT = "needs_input"
    RUNNING = "running"
    UNKNOWN = "unknown"


# Map intervention pattern types to EventType
_INPUT_TYPES = {"confirmation_prompt", "password_prompt", "overwrite_prompt", "continue_prompt"}
_ERROR_TYPES = {"fatal_error", "permission_error", "disk_error"}

# Patterns that indicate a command is still running
PROGRESS_PATTERNS = [
    re.compile(r'Downloading|Uploading|Compiling|Building|Installing', re.IGNORECASE),
    re.compile(r'\d+%'),
    re.compile(r'\.\.\.'),
    re.compile(r'ETA\s'),
    re.compile(r'^\s*\d+/\d+\s', re.MULTILINE),
]

_MAX_SUMMARY = 500
_RAW_TAIL = 1000  # last N chars kept in raw_output


@dataclass
class ScreenEvent:
    type: EventType
    summary: str
    needs_llm: bool
    exit_code: int | None = None
    raw_output: str = ""

    def __post_init__(self):
        if len(self.summary) > _MAX_SUMMARY:
            self.summary = self.summary[:_MAX_SUMMARY - 3] + "..."


class ScreenClassifier:
    """Classify a tmux screen capture into a ScreenEvent."""

    def classify(self, screen: str, exit_code: int | None = None) -> ScreenEvent:
        tail = screen[-_RAW_TAIL:] if len(screen) > _RAW_TAIL else screen

        # 1. Check intervention patterns
        for pattern, intervention_type in INTERVENTION_PATTERNS:
            if pattern.search(screen):
                if intervention_type in _INPUT_TYPES:
                    return ScreenEvent(
                        type=EventType.NEEDS_INPUT,
                        summary=f"Waiting for input: {intervention_type}",
                        needs_llm=True,
                        exit_code=exit_code,
                        raw_output=tail,
                    )
                if intervention_type in _ERROR_TYPES:
                    # Extract the matching line for context
                    hint = _extract_error_hint(screen, pattern)
                    return ScreenEvent(
                        type=EventType.ERROR,
                        summary=f"{intervention_type}: {hint}",
                        needs_llm=True,
                        exit_code=exit_code,
                        raw_output=tail,
                    )

        # 2. Exit code 0 → success
        if exit_code is not None and exit_code == 0:
            return ScreenEvent(
                type=EventType.SUCCESS,
                summary="Command completed successfully",
                needs_llm=False,
                exit_code=0,
                raw_output=tail,
            )

        # 3. Non-zero exit code → error
        if exit_code is not None and exit_code != 0:
            hint = _extract_error_hint_generic(screen)
            return ScreenEvent(
                type=EventType.ERROR,
                summary=f"Command failed (exit {exit_code}): {hint}",
                needs_llm=True,
                exit_code=exit_code,
                raw_output=tail,
            )

        # 4. Progress patterns → running
        for pattern in PROGRESS_PATTERNS:
            if pattern.search(screen):
                return ScreenEvent(
                    type=EventType.RUNNING,
                    summary="Command still running",
                    needs_llm=False,
                    exit_code=None,
                    raw_output=tail,
                )

        # 5. Prompt ready marker
        if "[AGENT_READY]" in screen:
            return ScreenEvent(
                type=EventType.SUCCESS,
                summary="Shell prompt ready",
                needs_llm=False,
                exit_code=exit_code,
                raw_output=tail,
            )

        # 6. Unknown
        return ScreenEvent(
            type=EventType.UNKNOWN,
            summary="Screen state unclear",
            needs_llm=True,
            exit_code=exit_code,
            raw_output=tail,
        )


def _extract_error_hint(screen: str, pattern: re.Pattern) -> str:
    """Extract the line matching the error pattern."""
    for line in screen.splitlines():
        if pattern.search(line):
            return line.strip()[:200]
    return ""


def _extract_error_hint_generic(screen: str) -> str:
    """Extract a useful error hint from screen output."""
    error_re = re.compile(r'(?:error|Error|ERROR|fatal|FATAL|failed|FAILED|traceback|Traceback)', re.IGNORECASE)
    for line in reversed(screen.splitlines()):
        stripped = line.strip()
        if stripped and error_re.search(stripped):
            return stripped[:200]
    # Fall back to last non-empty line
    for line in reversed(screen.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return "unknown error"


def format_event_for_llm(event: ScreenEvent) -> str:
    """Format event as compact message for LLM context."""
    if event.type == EventType.SUCCESS:
        code_part = f" exit:{event.exit_code}" if event.exit_code is not None else ""
        return f"[OK{code_part}] {event.summary}"
    elif event.type == EventType.ERROR:
        code_part = f" exit:{event.exit_code}" if event.exit_code is not None else ""
        return f"[ERROR{code_part}] {event.summary}"
    elif event.type == EventType.NEEDS_INPUT:
        return f"[NEEDS INPUT] {event.summary}"
    elif event.type == EventType.RUNNING:
        return f"[RUNNING] {event.summary}"
    else:
        return f"[SCREEN] {event.summary}"
