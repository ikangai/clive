# server/conversational.py
"""Conversational protocol handler for clive-to-clive communication.

Protocol:
  Input:  One task per line on stdin
  Output: TURN: thinking|done|failed|waiting
          CONTEXT: {json}
          DONE: {json}
          QUESTION: text
          PROGRESS: text
"""

import json
import logging
from typing import Callable

log = logging.getLogger(__name__)


class ConversationalHandler:
    """Handles the conversational protocol for inner clive instances."""

    def __init__(self, run_fn: Callable, emit_fn: Callable[[str], None],
                 toolset: str = "minimal", max_tokens: int = 50000):
        self.run_fn = run_fn
        self.emit_fn = emit_fn
        self.toolset = toolset
        self.max_tokens = max_tokens
        self._session_ctx = None

    def handle_task(self, task: str):
        """Process a single task through the conversational protocol."""
        self.emit_fn("TURN: thinking")

        try:
            result = self.run_fn(
                task,
                toolset_spec=self.toolset,
                max_tokens=self.max_tokens,
                session_ctx=self._session_ctx,
            )
            self.emit_fn(f"CONTEXT: {json.dumps({'result': result})}")
            self.emit_fn(f"DONE: {json.dumps({'status': 'success', 'summary': result.get('summary', str(result)) if isinstance(result, dict) else str(result)})}")
            self.emit_fn("TURN: done")
        except Exception as e:
            self.emit_fn(f"CONTEXT: {json.dumps({'error': str(e)})}")
            self.emit_fn("TURN: failed")
            log.error("Task failed: %s", e)

    def ask_question(self, question: str):
        """Emit a question to the caller and wait for response."""
        self.emit_fn("TURN: waiting")
        self.emit_fn(f"QUESTION: {question}")

    def run_loop(self, input_stream):
        """Run the conversational loop, reading tasks from input_stream.

        Reads one line at a time. Empty lines are skipped. EOF exits.
        """
        for line in input_stream:
            task = line.strip()
            if not task:
                continue
            self.handle_task(task)
