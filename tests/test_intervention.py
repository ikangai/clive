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
