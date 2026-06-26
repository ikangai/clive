"""Tests for intervention detection patterns."""
from completion import INTERVENTION_PATTERNS


def test_detects_yn_prompt():
    text = "Do you want to continue? [y/N]"
    matches = [(p, t) for p, t in INTERVENTION_PATTERNS if p.search(text)]
    assert len(matches) >= 1
    assert matches[0][1] == "confirmation_prompt"


def test_detects_password_prompt():
    text = "Password: "
    matches = [(p, t) for p, t in INTERVENTION_PATTERNS if p.search(text)]
    assert len(matches) >= 1
    assert matches[0][1] == "password_prompt"


def test_detects_overwrite():
    text = "File exists. Overwrite? "
    matches = [(p, t) for p, t in INTERVENTION_PATTERNS if p.search(text)]
    assert len(matches) >= 1
    assert matches[0][1] == "overwrite_prompt"


def test_detects_fatal_error():
    text = "FATAL: database connection failed"
    matches = [(p, t) for p, t in INTERVENTION_PATTERNS if p.search(text)]
    assert len(matches) >= 1
    assert matches[0][1] == "fatal_error"


def test_no_false_positive_on_normal_output():
    text = "Processing file 1 of 10...\nDone."
    matches = [(p, t) for p, t in INTERVENTION_PATTERNS if p.search(text)]
    assert len(matches) == 0


# --- pager / interactive-wedge detection (gh#40 follow-up) ---------------
# A pager (less/more) left a command wedged: the pane sits on a pager
# screen waiting for keystrokes, which the poll loop would otherwise read
# as "idle" forever. Detect the footer/prompt and surface it as an
# intervention so the wait loop can break and recover.


def _matched_types(text):
    return [t for p, t in INTERVENTION_PATTERNS if p.search(text)]


def test_detects_more_pager():
    # `more` footer.
    text = "line one\nline two\nline three\n--More--(40%)"
    assert "pager_prompt" in _matched_types(text)


def test_detects_less_end_marker():
    # `less` at end-of-file shows "(END)".
    text = "log line a\nlog line b\nlog line c\n(END)"
    assert "pager_prompt" in _matched_types(text)


def test_detects_lines_footer():
    # A "lines N-N" status footer (e.g. `more`/`less` ruler).
    text = "alpha\nbeta\ngamma\nlines 1-24"
    assert "pager_prompt" in _matched_types(text)


def test_detects_lone_colon_prompt():
    # `less` mid-file prompt is a lone ":" on the bottom row.
    text = "first\nsecond\nthird\n:"
    assert "pager_prompt" in _matched_types(text)


def test_pager_prompt_returns_correct_type():
    text = "some output\n--More--"
    matches = [(p, t) for p, t in INTERVENTION_PATTERNS if p.search(text)]
    assert any(t == "pager_prompt" for _, t in matches)


def test_no_pager_false_positive_on_normal_screen():
    # A normal shell screen ending at a prompt must not look like a pager.
    text = (
        "$ ls -la\n"
        "total 8\n"
        "drwxr-xr-x  4 user group  128 Jun 26 10:00 .\n"
        "-rw-r--r--  1 user group   42 Jun 26 10:00 notes.txt\n"
        "$ "
    )
    assert "pager_prompt" not in _matched_types(text)


def test_lone_colon_not_triggered_by_inline_colon():
    # A colon that is not alone on the bottom row (e.g. "Note:") is fine.
    text = "Building target\nNote: this is informational\nDone."
    assert "pager_prompt" not in _matched_types(text)
