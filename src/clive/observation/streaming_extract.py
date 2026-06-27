# streaming_extract.py
"""Early DONE detection during LLM streaming.

When the LLM says "DONE: summary", it typically follows with 50-200
tokens of explanation we don't need.  By detecting DONE during
streaming and signalling chat_stream to abort, we save those tokens
(cost) and the generation time (latency).

Usage with chat_stream's should_stop parameter:

    detector = EarlyDoneDetector()
    reply, pt, ct = chat_stream(
        client, messages,
        on_token=detector.feed,
        should_stop=detector.should_stop,
    )
    # reply contains content up to and including the DONE line
    # remaining tokens were never generated
"""

import re

_DONE_RE = re.compile(r'^DONE:\s*(.*)', re.MULTILINE)

# Fenced code blocks: a closed ```...``` pair, or a trailing unclosed fence
# (the common mid-stream case). Their contents are command bodies or pasted
# output, never a top-level completion signal — excise them before the DONE:
# search so a 'DONE:' the model is typing *inside* a command can't abort the
# stream mid-command. Mirrors command_extract._strip_fences (identical _DONE_RE).
_CLOSED_FENCE_RE = re.compile(r'```.*?```', re.DOTALL)
_OPEN_FENCE_RE = re.compile(r'```.*', re.DOTALL)


def _strip_fences(text: str) -> str:
    """Remove fenced code blocks so their contents can't trigger DONE detection."""
    return _OPEN_FENCE_RE.sub('', _CLOSED_FENCE_RE.sub('', text))


class EarlyDoneDetector:
    """Detects DONE: signal during streaming and signals early abort.

    Pass ``feed`` as the ``on_token`` callback and ``should_stop`` as
    the ``should_stop`` callback to ``chat_stream``.
    """

    def __init__(self):
        self.done_detected = False

    def feed(self, accumulated: str) -> None:
        """Called with the accumulated response so far."""
        if not self.done_detected and _DONE_RE.search(_strip_fences(accumulated)):
            self.done_detected = True

    def should_stop(self) -> bool:
        """Returns True when streaming should abort."""
        return self.done_detected
