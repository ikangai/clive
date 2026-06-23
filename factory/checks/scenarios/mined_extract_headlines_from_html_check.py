from factory.checks.check_base import CheckContext, CheckResult

EXPECTED = [
    "First headline about AI",
    "Second headline about terminals",
    "Third headline about agents",
]


def acceptance(ctx: CheckContext) -> CheckResult:
    import re

    raw = ctx.read_file("headlines.txt")

    # 1. Missing
    if raw is None:
        return CheckResult(
            False,
            "headlines.txt does not exist in the working directory",
            evidence={"path": "headlines.txt", "raw": None},
        )

    # 2. Empty / whitespace-only
    if raw.strip() == "":
        return CheckResult(
            False,
            "headlines.txt is empty (no headline content)",
            evidence={"raw": raw},
        )

    # Trim the overall content, then split. splitlines() handles \n and \r\n.
    # Strip each line to tolerate trailing/leading whitespace per line, but keep
    # internal blank lines (so they count against the strict "exactly three" rule).
    content = raw.strip()
    lines = [ln.strip() for ln in content.splitlines()]

    # 3. Reject any HTML markup — the task forbids tags / surrounding markup.
    tag_re = re.compile(r"<[^>]+>")
    tagged = [ln for ln in lines if tag_re.search(ln)]
    if tagged:
        return CheckResult(
            False,
            f"content contains HTML markup, expected plain text only: {tagged[0]!r}",
            evidence={"raw": raw, "lines": lines, "tagged_lines": tagged},
        )

    # 4. Exactly three lines (in order) — strict on count.
    if len(lines) != len(EXPECTED):
        return CheckResult(
            False,
            f"expected exactly {len(EXPECTED)} headline lines, found {len(lines)}",
            evidence={"raw": raw, "lines": lines, "expected": EXPECTED},
        )

    # 5. Exact content match, in document order.
    mismatches = []
    for i, (got, want) in enumerate(zip(lines, EXPECTED)):
        if got != want:
            mismatches.append({"index": i, "got": got, "want": want})

    if mismatches:
        return CheckResult(
            False,
            f"headline lines do not match expected content/order; first mismatch: {mismatches[0]}",
            evidence={
                "raw": raw,
                "lines": lines,
                "expected": EXPECTED,
                "mismatches": mismatches,
            },
        )

    return CheckResult(
        True,
        "headlines.txt contains exactly the three expected headlines, in document order, with no HTML markup",
        evidence={"lines": lines, "expected": EXPECTED},
    )
