"""Acceptance check for `hard-frequency-rank`.

Correct ranking (freq desc, alphabetical asc tie-break), exact `word count` lines:
    apple 3 / banana 2 / cherry 2 / date 1
The banana-before-cherry tie-break is the discriminator. Reads only the real
end-state file; never trusts the model's claim.
"""
from __future__ import annotations

from factory.checks.check_base import CheckContext, CheckResult

EXPECTED = ["apple 3", "banana 2", "cherry 2", "date 1"]


def acceptance(ctx: CheckContext) -> CheckResult:
    content = ctx.read_file("ranked.txt")
    if content is None:
        return CheckResult(False, "ranked.txt was not created", evidence={"present": False})
    # Tolerate trailing whitespace/newlines per line + at EOF; collapse internal
    # runs of spaces to one (so "apple  3" still reads as "apple 3"); strict on order.
    lines = [" ".join(ln.split()) for ln in content.strip().splitlines()]
    lines = [ln for ln in lines if ln != ""]
    ev = {"raw": content, "parsed": lines, "expected": EXPECTED}
    if lines == EXPECTED:
        return CheckResult(True, "ranked.txt is correct (freq desc, alpha tie-break, "
                           "banana before cherry)", evidence=ev)
    if len(lines) != len(EXPECTED):
        return CheckResult(False, f"expected {len(EXPECTED)} lines, got {len(lines)}", evidence=ev)
    if sorted(lines) == sorted(EXPECTED):
        return CheckResult(False, "right counts, wrong order — tie-break likely missed "
                           "(banana/cherry)", evidence=ev)
    bad = next((i for i, (a, b) in enumerate(zip(lines, EXPECTED)) if a != b), None)
    return CheckResult(False, f"mismatch at line {(bad or 0)+1}: got {lines[bad]!r}, "
                       f"want {EXPECTED[bad]!r}" if bad is not None else "content mismatch",
                       evidence=ev)
