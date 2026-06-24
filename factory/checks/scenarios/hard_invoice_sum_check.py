"""Acceptance check for `hard-invoice-sum`.

Correct total = 375: paid+3-field rows 1(100), 3(200), 6(75). Row 2 is pending,
row 4 is malformed (2 fields), row 5 is 'Paid' (case mismatch) — all excluded.
Reads only the real end-state file; never trusts the model's own claim.
"""
from __future__ import annotations

from factory.checks.check_base import CheckContext, CheckResult

EXPECTED = "375"


def acceptance(ctx: CheckContext) -> CheckResult:
    content = ctx.read_file("total.txt")
    if content is None:
        return CheckResult(False, "total.txt was not created", evidence={"present": False})
    got = content.strip()
    ev = {"raw": content, "got": got, "expected": EXPECTED}
    if got == EXPECTED:
        return CheckResult(True, "total.txt = 375 (paid 3-field rows summed; malformed "
                           "and 'Paid'-case rows correctly excluded)", evidence=ev)
    # Common wrong answers, for an auditable verdict.
    hint = ""
    if got in ("1374", "1374.0"):
        hint = " (looks like the 'Paid'/999 row was wrongly included)"
    elif got in ("350",):
        hint = " (looks like a paid row was dropped)"
    return CheckResult(False, f"total.txt = {got!r}, expected '375'{hint}", evidence=ev)
