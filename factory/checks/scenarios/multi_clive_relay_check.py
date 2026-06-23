"""Acceptance check for `multi-clive-relay`.

Verifies the WORLD result: the receiver wrote the exact token to relayed.txt.
Optionally asserts the channel actually carried the message (the room transcript
shows the token), because a delivered-but-not-acted message — or an acted message
that never traveled — would both be a claim standing in for a fact (§12).
"""
from __future__ import annotations

from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    token = (ctx.scenario.get("token") or "").strip()
    content = ctx.read_file("relayed.txt")
    ev = {"expected_token": token, "present": content is not None,
          "head": (content or "")[:120]}

    if content is None:
        return CheckResult(False, "relayed.txt was not created — the relay did not "
                           "complete the world result", evidence=ev)
    world_ok = content.strip() == token
    # Secondary assertion: did the channel actually carry it? (room transcript)
    room_log = ctx.extra.get("room_transcript", "")
    ev["channel_carried"] = token in room_log if room_log else None

    if not world_ok:
        return CheckResult(False, f"relayed.txt held {content.strip()!r}, expected "
                           f"{token!r}", evidence=ev)
    if room_log and token not in room_log:
        # World artefact is right but the channel never carried the token — a
        # fabricated end-state. Fail: the coordination is what's under test.
        return CheckResult(False, "relayed.txt is correct but the room transcript "
                           "never carried the token (fabricated end-state)", evidence=ev)
    return CheckResult(True, "token relayed A -> room -> B -> disk", evidence=ev)
