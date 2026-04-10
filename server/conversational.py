# server/conversational.py
"""Conversational protocol handler for clive-to-clive communication.

Emits framed sentinel messages (see protocol.py). Legacy DONE: / TURN: /
CONTEXT: / QUESTION: line prefixes are gone — framed only.

  Input:  One task per line on stdin
  Output: framed <<<CLIVE:turn:...>>>, <<<CLIVE:context:...>>>,
          <<<CLIVE:question:...>>>, <<<CLIVE:progress:...>>>
"""

import logging
from typing import Callable

from protocol import encode

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
        self.emit_fn(encode("turn", {"state": "thinking"}))

        try:
            result = self.run_fn(
                task,
                toolset_spec=self.toolset,
                max_tokens=self.max_tokens,
                session_ctx=self._session_ctx,
            )
            self.emit_fn(encode("context", {"result": result}))
            self.emit_fn(encode("turn", {"state": "done"}))
        except Exception as e:
            self.emit_fn(encode("context", {"error": str(e)}))
            self.emit_fn(encode("turn", {"state": "failed"}))
            log.error("Task failed: %s", e)

    def ask_question(self, question: str):
        """Emit a question frame and signal that we are waiting for input."""
        self.emit_fn(encode("turn", {"state": "waiting"}))
        self.emit_fn(encode("question", {"text": question}))

    def run_loop(self, input_stream):
        """Run the conversational loop, reading tasks from input_stream.

        Reads one line at a time. Empty lines are skipped. EOF exits.
        """
        for line in input_stream:
            task = line.strip()
            if not task:
                continue
            self.handle_task(task)
