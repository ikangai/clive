from factory.checks.check_base import CheckContext, CheckResult

EXPECTED = [
    "gail@example.com",
    "finn@example.com",
    "eve@example.com",
    "dan@example.com",
    "cara@example.com",
]


def acceptance(ctx: CheckContext) -> CheckResult:
    """Verify senders.txt holds the From addresses of the 5 most-recent emails,
    most-recent-first, with the two oldest (amy@, ben@) excluded.

    Reads only the real end-state file; never trusts a model-reported success.
    """
    raw = ctx.read_file("senders.txt")

    # Missing / empty / non-string cases first — return a clear False.
    if raw is None:
        return CheckResult(
            False,
            "senders.txt does not exist in the working directory",
            evidence={"senders_txt": None},
        )
    if not isinstance(raw, str) or raw.strip() == "":
        return CheckResult(
            False,
            "senders.txt is empty or unreadable",
            evidence={"raw": raw, "raw_len": len(raw) if isinstance(raw, str) else None},
        )

    # Tolerant on formatting: strip the whole blob, split on newlines, strip each
    # line, and drop blank lines (trailing newline / blank-line padding is benign).
    # Strict on the substantive criterion: the 5 addresses and their order.
    lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln != ""]

    evidence = {
        "raw": raw,
        "parsed_lines": lines,
        "expected_lines": EXPECTED,
    }

    if len(lines) != len(EXPECTED):
        return CheckResult(
            False,
            f"expected exactly {len(EXPECTED)} non-empty lines, found {len(lines)}: {lines}",
            evidence=evidence,
        )

    # Excluded-senders guard: the two oldest must never appear, in any position.
    excluded = {"amy@example.com", "ben@example.com"}
    leaked = sorted(excluded.intersection(lines))
    if leaked:
        return CheckResult(
            False,
            f"excluded (oldest) senders present in output: {leaked}",
            evidence=evidence,
        )

    if lines != EXPECTED:
        # Pinpoint the first divergence for a human-auditable verdict.
        first_bad = next(
            (i for i, (a, b) in enumerate(zip(lines, EXPECTED)) if a != b),
            None,
        )
        detail = "senders.txt content/order does not match expected"
        if first_bad is not None:
            detail += (
                f" — line {first_bad + 1}: got {lines[first_bad]!r}, "
                f"expected {EXPECTED[first_bad]!r}"
            )
        return CheckResult(False, detail, evidence=evidence)

    return CheckResult(
        True,
        "senders.txt holds the 5 most-recent senders, most-recent-first, "
        "with amy@ and ben@ correctly excluded",
        evidence=evidence,
    )
