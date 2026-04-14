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


class EarlyDoneDetector:
    """Detects DONE: signal during streaming and signals early abort.

    Pass ``feed`` as the ``on_token`` callback and ``should_stop`` as
    the ``should_stop`` callback to ``chat_stream``.
    """

    def __init__(self):
        self.done_detected = False

    def feed(self, accumulated: str) -> None:
        """Called with the accumulated response so far."""
        if not self.done_detected and _DONE_RE.search(accumulated):
            self.done_detected = True

    def should_stop(self) -> bool:
        """Returns True when streaming should abort."""
        return self.done_detected
