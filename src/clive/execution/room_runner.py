"""Client-side room-turn runner.

Invoked by a member clive when it receives a ``your_turn`` frame from
the lobby. Pure w.r.t. IO — given the parsed payload and a chat-
capable LLM client, returns the single ``(kind, payload)`` pair to
emit back as a ``say`` or ``pass`` frame.

The three public functions are separated so each is independently
testable:

    load_driver()            -> str            # read drivers/room.md
    build_messages(payload, driver_text) -> list[{role,content}]
    parse_response(content, thread_id) -> (kind, payload)
    decide_turn(payload, llm_client, ...) -> (kind, payload)   # = compose

See docs/plans/2026-04-14-clive-rooms-design.md §6.2 and §6.4.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# drivers/ lives under src/clive/. We resolve relative to this file so
# the runner works regardless of the caller's cwd and regardless of
# whether the package is imported from source or an installed wheel.
_DRIVER_PATH = Path(__file__).resolve().parent.parent / "drivers" / "room.md"


def load_driver() -> str:
    """Read the static room driver. No templating (§6.4)."""
    return _DRIVER_PATH.read_text(encoding="utf-8")


# ─── Prompt construction ────────────────────────────────────────────────────


def _format_recent(recent: list[dict]) -> str:
    lines = []
    for m in recent:
        frm = m.get("from", "?")
        kind = m.get("kind", "say")
        if kind == "pass":
            lines.append(f"{frm}: (pass)")
        else:
            body = m.get("body", "")
            lines.append(f"{frm}: {body}")
    return "\n".join(lines)


def build_messages(payload: dict, driver_text: str) -> list[dict]:
    """Assemble the chat-API messages list per §6.2.

    Layout:
      system: <driver_text>
      user:   <structured header>
              <summary? only when present>
              <recent messages as `from: body` / `from: (pass)`>
              <closing instruction>
    """
    name = payload.get("name", "")
    thread_id = payload.get("thread_id", "")
    room = payload.get("room", "")
    members = payload.get("members", [])
    summary = payload.get("summary")
    recent = payload.get("recent", [])

    header_lines = [
        f"name: {name}",
        f"thread_id: {thread_id}",
        f"room: {room}",
        f"members: {', '.join(members)}",
    ]
    parts = ["\n".join(header_lines)]

    if summary:
        parts.append(f"Earlier in this thread:\n{summary}")

    if recent:
        parts.append("Recent messages:\n" + _format_recent(recent))
    else:
        # Explicit "no recent" signal so the LLM doesn't hallucinate
        # missing context. Happens on the initiator's first turn when
        # they opened without a prompt.
        parts.append("Recent messages: (none)")

    parts.append(
        "Respond with exactly one of `say: <body>` or `pass:` followed "
        "by `DONE:` on its own line."
    )
    user_content = "\n\n".join(parts)
    return [
        {"role": "system", "content": driver_text},
        {"role": "user", "content": user_content},
    ]


# ─── Response parsing ───────────────────────────────────────────────────────


# First line that starts (after optional whitespace) with `say:` or
# `pass:` (case-insensitive) is the directive. `(?ms)` = multiline,
# dotall not needed since we match per-line.
_DIRECTIVE_RE = re.compile(r"^\s*(say|pass)\s*:(.*)$", re.IGNORECASE | re.MULTILINE)


def parse_response(content: str, *, thread_id: str) -> tuple[str, dict]:
    """Parse an LLM response into the frame kind + payload to emit.

    Degradation policy: any malformed output — no directive, empty
    say body, multiple directives, missing `DONE:` — degrades to
    ``pass`` rather than emitting a frame the lobby would nack.
    Emitting a nacked frame wastes the turn and produces noise; a
    clean pass keeps the rotation moving with the same net outcome.
    """
    m = _DIRECTIVE_RE.search(content)
    if m is None:
        return "pass", {"thread_id": thread_id}

    directive = m.group(1).lower()
    tail = m.group(2).strip()   # trailing text on the directive line

    if directive == "pass":
        return "pass", {"thread_id": thread_id}

    # say: gather the body — everything from the tail of the directive
    # line through any subsequent lines until DONE: or EOF. If the
    # LLM wraps the body across lines, keep the wrap (a human reading
    # scrollback should see what the LLM said, not a truncation).
    start = m.end()
    # Skip the newline that terminated the directive line — it's a
    # delimiter, not a blank line in the body. Blank lines AFTER it
    # (e.g., inside a code block) are preserved.
    rest = content[start:]
    if rest.startswith("\n"):
        rest = rest[1:]
    # Stop at DONE: on its own-ish line, or at another directive line.
    body_lines = [tail] if tail else []
    for line in rest.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DONE:"):
            break
        if _DIRECTIVE_RE.match(line):
            break
        body_lines.append(line)
    body = "\n".join(body_lines).strip()

    if not body:
        # Driver violation: `say:` with empty body. Degrade to pass.
        return "pass", {"thread_id": thread_id}

    return "say", {"thread_id": thread_id, "body": body}


# ─── decide_turn — the composed entry point ─────────────────────────────────


def decide_turn(
    payload: dict,
    *,
    llm_client,
    driver_text: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 512,
) -> tuple[str, dict]:
    """End-to-end turn decision: build prompt → call LLM → parse.

    ``driver_text`` defaults to the packaged ``drivers/room.md``; tests
    can inject a short stand-in. ``max_tokens`` is intentionally small
    because room messages are short — a higher cap makes a
    misbehaving/chatty model produce multi-paragraph filler, which the
    driver forbids anyway and which the lobby would slow-path.
    """
    from llm import chat

    if driver_text is None:
        driver_text = load_driver()

    messages = build_messages(payload, driver_text=driver_text)
    try:
        content, _pt, _ct = chat(
            llm_client, messages, max_tokens=max_tokens, model=model,
        )
    except Exception as e:
        _log.warning("room_runner: llm.chat failed (%s) — passing turn", e)
        return "pass", {"thread_id": payload.get("thread_id", "")}

    return parse_response(content, thread_id=payload.get("thread_id", ""))
