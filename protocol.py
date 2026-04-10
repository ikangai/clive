"""Framed conversational protocol for clive-to-clive communication.

Frame format (single line, self-delimiting):

    <<<CLIVE:{kind}:{base64(json(payload))}>>>

The base64 wrapping guarantees that protocol sentinels cannot be spoofed
by stray tool output or LLM-generated text: the marker characters
('<', '>', ':') cannot appear inside a base64-encoded payload, so a
partial match on the literal sentinel string will fail to decode and
be dropped.

Replaces the legacy line-prefix parsing in remote.py.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

_PREFIX = "<<<CLIVE:"
_SUFFIX = ">>>"

# Source of truth for frame kinds. Anything else is rejected at decode time.
KINDS = frozenset({
    "turn",          # payload: {"state": "thinking|waiting|done|failed"}
    "context",       # payload: arbitrary dict (result, error, etc.)
    "question",      # payload: {"text": "..."}
    "file",          # payload: {"name": "..."}
    "progress",      # payload: {"text": "..."}
    "llm_request",   # payload: {"id": "...", "messages": [...], "model": "...", "max_tokens": N}
    "llm_response",  # payload: {"id": "...", "content": "...", "prompt_tokens": N, "completion_tokens": N}
    "llm_error",     # payload: {"id": "...", "error": "..."}
    "alive",         # payload: {"ts": <float>}
})

# Strict frame regex: kind is lowercase alphanumeric/underscore, payload is
# base64 alphabet. The regex does not contain '<' or '>' inside the body,
# so it cannot match across sentinel boundaries.
_FRAME_RE = re.compile(
    r"<<<CLIVE:(?P<kind>[a-z_]+):(?P<b64>[A-Za-z0-9+/=]+)>>>"
)


@dataclass(frozen=True)
class Frame:
    kind: str
    payload: dict


def encode(kind: str, payload: dict) -> str:
    """Encode a payload as a framed protocol message.

    Returns a single line (no trailing newline) suitable for print() with flush.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown frame kind: {kind!r}")
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"{_PREFIX}{kind}:{b64}{_SUFFIX}"


def decode_all(screen: str) -> list[Frame]:
    """Extract all valid frames from a screen blob, in order of appearance.

    Silently drops:
      - frames with unknown kinds
      - frames whose payload is not valid base64
      - frames whose decoded payload is not valid JSON
      - frames whose payload is not a JSON object
    """
    frames: list[Frame] = []
    for m in _FRAME_RE.finditer(screen):
        kind = m.group("kind")
        if kind not in KINDS:
            continue
        try:
            raw = base64.b64decode(m.group("b64"), validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        frames.append(Frame(kind=kind, payload=payload))
    return frames


def latest(frames: list[Frame], kind: str) -> Frame | None:
    """Return the most recent frame of a given kind, or None."""
    for f in reversed(frames):
        if f.kind == kind:
            return f
    return None
