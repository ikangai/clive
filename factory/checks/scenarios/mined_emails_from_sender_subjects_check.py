from factory.checks.check_base import CheckContext, CheckResult

EXPECTED = ["March recap", "Model roundup", "Weekend reading"]
EXCLUDED = ["Newsletter", "Weekly digest"]


def acceptance(ctx: CheckContext) -> CheckResult:
    raw = ctx.read_file("subjects.txt")

    # Missing
    if raw is None:
        return CheckResult(
            False,
            "subjects.txt does not exist in the working directory",
            evidence={"raw": None, "expected": EXPECTED},
        )

    # Empty / whitespace-only
    if raw.strip() == "":
        return CheckResult(
            False,
            "subjects.txt is empty (no subject lines written)",
            evidence={"raw": raw, "expected": EXPECTED},
        )

    # Parse: drop leading/trailing blank lines around the block, then take each
    # line trimmed of surrounding whitespace. We do NOT silently filter interior
    # blank lines — an extra blank line in the middle is a real malformation and
    # should fail against the "exactly three lines" criterion.
    lines = [ln.strip() for ln in raw.strip().split("\n")]

    # Line-count check
    if len(lines) != len(EXPECTED):
        return CheckResult(
            False,
            "subjects.txt has %d line(s); expected exactly %d"
            % (len(lines), len(EXPECTED)),
            evidence={
                "raw": raw,
                "parsed_lines": lines,
                "expected": EXPECTED,
            },
        )

    # Exact ordered content check
    if lines != EXPECTED:
        # Surface specifically helpful diagnostics: leaked 'Subject:' prefixes,
        # included non-matching senders, or wrong order.
        leaked_prefix = [ln for ln in lines if ln.lower().startswith("subject:")]
        included_excluded = [ln for ln in lines if ln in EXCLUDED]
        if set(lines) == set(EXPECTED):
            detail = "correct subjects but wrong order; expected %r, got %r" % (
                EXPECTED,
                lines,
            )
        elif leaked_prefix:
            detail = "subject lines retain the 'Subject:' prefix: %r" % leaked_prefix
        elif included_excluded:
            detail = "non-matching senders were not excluded: %r" % included_excluded
        else:
            detail = "content does not match; expected %r, got %r" % (EXPECTED, lines)
        return CheckResult(
            False,
            detail,
            evidence={
                "raw": raw,
                "parsed_lines": lines,
                "expected": EXPECTED,
                "leaked_subject_prefix": leaked_prefix,
                "included_excluded_senders": included_excluded,
            },
        )

    return CheckResult(
        True,
        "subjects.txt contains exactly the three expected subjects in ascending "
        "filename order, with non-matching senders excluded",
        evidence={
            "raw": raw,
            "parsed_lines": lines,
            "expected": EXPECTED,
        },
    )
