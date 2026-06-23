"""Acceptance check for the demo scenario `machine-status`.

Reads status.txt from the real end-state and requires a machine-readable
`RESULT=OK` line. The goal only asks for a "status report", so the current
champion writes prose and FAILS this; a one-change candidate whose system_prompt
teaches the RESULT= convention PASSES — exactly the champion-fails / candidate-
fixes shape that lights up the promotion gate.
"""
from __future__ import annotations

import re

from factory.checks.check_base import CheckContext, CheckResult

_RESULT_LINE = re.compile(r"(?mi)^\s*RESULT=OK\s*$")


def acceptance(ctx: CheckContext) -> CheckResult:
    content = ctx.read_file("status.txt")
    if content is None:
        return CheckResult(False, "status.txt was not created", evidence={"present": False})
    ev = {"present": True, "head": content[:200], "bytes": len(content)}
    if _RESULT_LINE.search(content):
        return CheckResult(True, "status.txt carries the machine-readable RESULT=OK line",
                           evidence=ev)
    return CheckResult(False, "status.txt has no machine-readable `RESULT=OK` line "
                       "(prose-only report)", evidence=ev)
