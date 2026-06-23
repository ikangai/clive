"""Acceptance check for scenario `hello-artifact`.

Reads the real end-state: factory/.../work/report.txt must have line 1 exactly
`STATUS: OK` and line 2 an all-digits epoch. The candidate clive's own DONE:
claim is never consulted.
"""
from __future__ import annotations

from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    content = ctx.read_file("report.txt")
    if content is None:
        return CheckResult(False, "report.txt was not created in the workdir",
                           evidence={"file": "report.txt", "present": False})
    lines = content.splitlines()
    ev = {"file": "report.txt", "present": True, "line_count": len(lines),
          "head": content[:200]}
    if len(lines) < 2:
        return CheckResult(False, f"expected >=2 lines, got {len(lines)}", evidence=ev)
    if lines[0].strip() != "STATUS: OK":
        return CheckResult(False, f"line 1 was {lines[0]!r}, expected 'STATUS: OK'",
                           evidence=ev)
    if not lines[1].strip().isdigit():
        return CheckResult(False, f"line 2 was {lines[1]!r}, expected an epoch integer",
                           evidence=ev)
    return CheckResult(True, "report.txt has STATUS: OK + an epoch timestamp", evidence=ev)
