"""L2 byte-stream classifier.

Scans raw tmux pane bytes (pre-render, with ANSI escapes intact) for
high-signal patterns: SGR alert colors, known prompts, error keywords,
and Clive's own command-end markers. Emits ByteEvent for each match.

Stateless across invocations except for:
  - _carryover: last (MAX_PATTERN_LEN - 1) bytes, to catch patterns
    split across chunk boundaries.
  - _last_emitted_pos: per-kind monotonic byte offset of the most
    recent match, to avoid double-firing when feed() is called with
    overlapping data. Keyed by kind so an earlier pattern's match at
    a high offset doesn't suppress a later pattern's match at a lower
    offset within the same chunk.
"""
import re
import time
from dataclasses import dataclass


MAX_PATTERN_LEN = 128


BYTE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rb'\x1b\[[0-9;]*3[13]m'),               "color_alert"),
    (re.compile(rb'\x1b\[[0-9;]*4[13]m'),               "color_bg_alert"),
    (re.compile(rb'\x1b\[[0-9;]*5m'),                   "blink_attr"),
    (re.compile(rb'(?:^|[^\w])[Pp]assword\s*:'),        "password_prompt"),
    (re.compile(rb'\[y/N\]|\[Y/n\]'),                   "confirm_prompt"),
    (re.compile(rb'Are you sure'),                      "confirm_prompt"),
    (re.compile(rb'Traceback|FATAL|panic:'),            "error_keyword"),
    (re.compile(rb'Permission denied'),                 "permission_error"),
    # cmd_end: \d+ is intentional — prevents matching unexpanded echoes
    # like "EXIT:$? ___DONE_..." (which would be a command echo, not a
    # real completion marker).
    (re.compile(rb'EXIT:\d+ ___DONE_'),                 "cmd_end"),
]


@dataclass
class ByteEvent:
    kind: str
    match_bytes: bytes
    stream_offset: int
    timestamp: float


class ByteClassifier:
    def __init__(self):
        self._carryover = b""
        self._stream_pos = 0
        self._last_emitted_pos: dict[str, int] = {}

    def feed(self, chunk: bytes) -> list[ByteEvent]:
        if not chunk and not self._carryover:
            return []
        window = self._carryover + chunk
        window_base = self._stream_pos - len(self._carryover)
        events: list[ByteEvent] = []

        for pattern, kind in BYTE_PATTERNS:
            for m in pattern.finditer(window):
                abs_pos = window_base + m.start()
                if abs_pos <= self._last_emitted_pos.get(kind, -1):
                    continue
                events.append(ByteEvent(
                    kind=kind,
                    match_bytes=m.group(0),
                    stream_offset=abs_pos,
                    timestamp=time.monotonic(),
                ))
                self._last_emitted_pos[kind] = max(
                    self._last_emitted_pos.get(kind, -1), abs_pos
                )

        self._stream_pos += len(chunk)
        tail_len = min(MAX_PATTERN_LEN - 1, len(window))
        self._carryover = window[-tail_len:] if tail_len else b""
        return events
