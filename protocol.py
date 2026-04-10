"""Framed conversational protocol for clive-to-clive communication.

Frame format (single line, self-delimiting):

    <<<CLIVE:{kind}:{nonce}:{base64(json(payload))}>>>

The outer clive generates a random session nonce and injects it into
the inner clive via the ``CLIVE_FRAME_NONCE`` environment variable.
Every frame the inner emits carries that nonce; the outer rejects
frames whose nonce does not match. An LLM running inside the inner
cannot fabricate a valid frame because the nonce is not present in any
prompt it can see — it is an env var, not part of the reasoning
context.

The base64 wrapping guarantees that protocol sentinels cannot be
spoofed by stray tool output: the marker characters ('<', '>', ':')
cannot appear inside a base64-encoded payload, so a partial match on
the literal sentinel string will fail to decode and be dropped. Base64
alone is NOT enough to stop an adversarial LLM (which can produce a
valid base64 string by design); the nonce is what closes that gap.

The nonce alphabet is urlsafe-base64 minus padding (``[A-Za-z0-9_-]``).
An empty nonce is allowed and means "unauthenticated frame" — useful
for unit tests and dev paths where no security boundary exists. In
production the outer always generates and injects a non-empty nonce.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass

_log = logging.getLogger(__name__)

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

# Strict frame regex. `kind` is lowercase alphanumeric/underscore,
# `nonce` is urlsafe-b64 alphabet (possibly empty), `b64` is standard
# base64. The explicit ':' delimiters and absence of '<' / '>' in any
# body class guarantee the regex cannot match across sentinel
# boundaries.
_FRAME_RE = re.compile(
    r"<<<CLIVE:(?P<kind>[a-z_]+):(?P<nonce>[A-Za-z0-9_-]*):(?P<b64>[A-Za-z0-9+/=]+)>>>"
)

# Alphabet enforcement for outgoing nonces. We check encode-time nonce
# against this class to prevent injection of `:` or `>` into a frame.
_NONCE_ALPHABET = re.compile(r"\A[A-Za-z0-9_-]*\Z")

_ENV_NONCE = "CLIVE_FRAME_NONCE"


@dataclass(frozen=True)
class Frame:
    kind: str
    payload: dict


def _current_nonce_from_env() -> str:
    """Read the session nonce from the CLIVE_FRAME_NONCE env var.

    Returns empty string if unset. This is the "inner" side's default
    source: exporting the env var once at process startup is enough to
    make every emitter authenticate its frames.
    """
    return os.environ.get(_ENV_NONCE, "")


def encode(kind: str, payload: dict, nonce: str | None = None) -> str:
    """Encode a payload as a framed protocol message.

    If ``nonce`` is None, the CLIVE_FRAME_NONCE env var is consulted;
    an unset env var produces an empty nonce (unauthenticated frame).

    Returns a single line (no trailing newline) suitable for print() with flush.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown frame kind: {kind!r}")
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    if nonce is None:
        nonce = _current_nonce_from_env()
    if not _NONCE_ALPHABET.match(nonce):
        raise ValueError(
            f"nonce contains disallowed characters: {nonce!r} "
            f"(alphabet is [A-Za-z0-9_-])"
        )
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"{_PREFIX}{kind}:{nonce}:{b64}{_SUFFIX}"


def decode_all(screen: str, nonce: str = "") -> list[Frame]:
    """Extract all valid frames from a screen blob, in order of appearance.

    Only frames whose nonce exactly matches the ``nonce`` argument are
    returned; all others are silently dropped. Callers that read panes
    of a specific inner clive must pass that inner's injected nonce.
    Tests and dev paths default to the empty nonce.

    Silently drops:
      - frames with unknown kinds
      - frames with mismatched nonce
      - frames whose payload is not valid base64
      - frames whose decoded payload is not valid JSON
      - frames whose payload is not a JSON object

    Every drop is logged at DEBUG level so misuse is visible when you
    turn on debug logging.
    """
    frames: list[Frame] = []
    for m in _FRAME_RE.finditer(screen):
        kind = m.group("kind")
        if kind not in KINDS:
            _log.debug("dropping frame with unknown kind: %r", kind)
            continue
        frame_nonce = m.group("nonce")
        if frame_nonce != nonce:
            _log.debug("dropping %s frame: nonce mismatch (want %r, got %r)",
                       kind, nonce, frame_nonce)
            continue
        b64 = m.group("b64")
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as e:
            _log.debug("dropping %s frame: bad base64 (%s)", kind, e)
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            _log.debug("dropping %s frame: bad json (%s)", kind, e)
            continue
        if not isinstance(payload, dict):
            _log.debug("dropping %s frame: payload is %s, not dict",
                       kind, type(payload).__name__)
            continue
        frames.append(Frame(kind=kind, payload=payload))
    return frames


def latest(frames: list[Frame], kind: str) -> Frame | None:
    """Return the most recent frame of a given kind, or None."""
    for f in reversed(frames):
        if f.kind == kind:
            return f
    return None


def generate_nonce() -> str:
    """Generate a fresh 128-bit session nonce (urlsafe base64, no padding).

    The outer clive calls this once per inner it spawns and forwards
    the result via CLIVE_FRAME_NONCE. Not module-level because callers
    may want deterministic fakes in tests.
    """
    import secrets
    return secrets.token_urlsafe(16)  # 16 bytes → ~22 chars, urlsafe
