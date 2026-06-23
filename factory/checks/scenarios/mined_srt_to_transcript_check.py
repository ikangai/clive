from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    """Verify transcript.txt is exactly the three caption text lines, in order,
    with all sequence numbers, timestamp lines, and blank separators removed."""

    expected = [
        "Hello and welcome.",
        "Today we discuss clive.",
        "This is the final line.",
    ]

    raw = ctx.read_file("transcript.txt")

    # --- missing / empty / malformed cases first ---
    if raw is None:
        return CheckResult(
            False,
            "transcript.txt was not created in the working directory.",
            evidence={"transcript_exists": False},
        )

    if raw.strip() == "":
        return CheckResult(
            False,
            "transcript.txt exists but is empty (after stripping whitespace).",
            evidence={"transcript_exists": True, "raw_repr": repr(raw)},
        )

    # --- normalize: tolerate trailing newline(s) at EOF, strict on interior blanks ---
    lines = raw.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()  # drop only trailing blank line(s) from a final newline

    # tolerate trailing/leading whitespace per line; the substantive content is the text
    stripped = [ln.strip() for ln in lines]

    evidence = {
        "transcript_exists": True,
        "raw_repr": repr(raw),
        "parsed_lines": stripped,
        "line_count": len(stripped),
        "expected_lines": expected,
    }

    # --- reject any interior blank line (no blank lines allowed) ---
    if any(ln == "" for ln in stripped):
        return CheckResult(
            False,
            "transcript.txt contains a blank line; blank separators must be removed.",
            evidence=evidence,
        )

    # --- reject leftover SRT artifacts (sequence numbers / timestamp lines) ---
    leftover_seq = [ln for ln in stripped if ln.isdigit()]
    leftover_ts = [ln for ln in stripped if "-->" in ln]
    if leftover_seq or leftover_ts:
        return CheckResult(
            False,
            "transcript.txt still contains SRT artifacts: "
            f"sequence-number lines={leftover_seq!r}, timestamp lines={leftover_ts!r}.",
            evidence={**evidence, "leftover_seq": leftover_seq, "leftover_ts": leftover_ts},
        )

    # --- exact line count ---
    if len(stripped) != len(expected):
        return CheckResult(
            False,
            f"Expected exactly {len(expected)} text lines but found {len(stripped)}.",
            evidence=evidence,
        )

    # --- exact content, in order ---
    if stripped != expected:
        mismatches = [
            {"index": i, "got": g, "want": w}
            for i, (g, w) in enumerate(zip(stripped, expected))
            if g != w
        ]
        return CheckResult(
            False,
            "transcript.txt lines do not match the expected caption text in order.",
            evidence={**evidence, "mismatches": mismatches},
        )

    return CheckResult(
        True,
        "transcript.txt contains exactly the three caption text lines, in order, "
        "with sequence numbers, timestamp lines, and blank separators removed.",
        evidence=evidence,
    )
