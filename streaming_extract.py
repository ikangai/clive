# streaming_extract.py
"""Streaming command extraction — detect fenced bash blocks as LLM tokens arrive.

Used by the interactive runner to overlap LLM generation with command
execution. The detector fires a callback as soon as a complete ```bash
block is detected, without waiting for the full response.
"""

import re

_FENCED_SHELL_RE = re.compile(r'```(?:bash|sh)\s*\n(.*?)```', re.DOTALL)
_DONE_RE = re.compile(r'^DONE:\s*(.*)', re.MULTILINE)


class StreamingCommandDetector:
    """Accumulates streaming tokens and fires on_command when a bash block closes.

    Usage:
        detector = StreamingCommandDetector(on_command=lambda cmd: ...)
        chat_stream(client, messages, on_token=detector.feed)
        # on_command fires as soon as closing ``` is detected
    """

    def __init__(self, on_command=None):
        self._on_command = on_command
        self._fired = False
        self.done_detected = False

    def feed(self, accumulated: str) -> None:
        """Called with the accumulated response so far (not individual tokens).

        chat_stream's on_token callback passes the full accumulated text
        on each token, so we always have the complete response-so-far.
        """
        if not self._fired and _DONE_RE.search(accumulated):
            self.done_detected = True

        if self._fired:
            return

        m = _FENCED_SHELL_RE.search(accumulated)
        if m:
            self._fired = True
            cmd = m.group(1).strip()
            if cmd and self._on_command:
                self._on_command(cmd)
