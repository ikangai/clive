"""Acceptance check for the demo scenario `gate-demo`.

The goal only asks for status.txt; the check ALSO requires a sibling
status.txt.done completion receipt — a harness convention the current champion
prompt does not establish (so it fails), but a one-change candidate that teaches
clive to leave receipts clears it. Reads the real end-state; never trusts a claim.
"""
from __future__ import annotations

from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    report = ctx.read_file("status.txt")
    if report is None:
        return CheckResult(False, "status.txt was not created", evidence={"status_txt": False})
    receipt = ctx.read_file("status.txt.done")
    ev = {"status_txt": True, "receipt_present": receipt is not None, "head": report[:120]}
    if receipt is None:
        return CheckResult(False, "no completion receipt — a sibling status.txt.done file "
                           "must exist next to the report", evidence=ev)
    return CheckResult(True, "status.txt written + a status.txt.done completion receipt left",
                       evidence=ev)
