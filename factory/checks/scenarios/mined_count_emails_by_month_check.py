from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    """Verify counts.txt holds exactly 'march=3' then 'april=2'.

    Reads the real end-state file the task was supposed to produce; never
    trusts any model claim of success. March has 3 emails, April has 2, and
    the lone February email is a distractor that must not be counted.
    """
    content = ctx.read_file("counts.txt")

    # Missing file -> definitive fail.
    if content is None:
        return CheckResult(
            False,
            "counts.txt does not exist in the working directory",
            evidence={"counts.txt": None},
        )

    raw = content

    # Tolerate leading/trailing whitespace and trailing newlines around the
    # whole file, but be strict about the substantive two-line structure.
    stripped = content.strip()
    if not stripped:
        return CheckResult(
            False,
            "counts.txt is empty (only whitespace)",
            evidence={"raw": raw},
        )

    # Strip per-line trailing/leading whitespace; require EXACTLY two lines
    # after the overall strip (rejects internal blank lines or extra lines).
    lines = [ln.strip() for ln in stripped.split("\n")]

    if len(lines) != 2:
        return CheckResult(
            False,
            f"expected exactly 2 lines, got {len(lines)}",
            evidence={"raw": raw, "lines": lines},
        )

    expected_first = "march=3"
    expected_second = "april=2"
    first, second = lines[0], lines[1]

    if first != expected_first or second != expected_second:
        return CheckResult(
            False,
            (
                "content mismatch: "
                f"line1={first!r} (want {expected_first!r}), "
                f"line2={second!r} (want {expected_second!r})"
            ),
            evidence={
                "raw": raw,
                "line1": first,
                "line2": second,
                "expected_line1": expected_first,
                "expected_line2": expected_second,
            },
        )

    return CheckResult(
        True,
        "counts.txt contains exactly 'march=3' then 'april=2' (February distractor excluded)",
        evidence={"raw": raw, "line1": first, "line2": second},
    )
