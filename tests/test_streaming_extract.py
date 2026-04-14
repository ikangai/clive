# tests/test_streaming_extract.py
"""Tests for early DONE detection during streaming."""
from streaming_extract import EarlyDoneDetector


def test_detects_done_signal():
    d = EarlyDoneDetector()
    d.feed("DONE: task complete")
    assert d.done_detected
    assert d.should_stop()


def test_no_done_stays_false():
    d = EarlyDoneDetector()
    d.feed("I'll run ls now.\n```bash\nls\n```")
    assert not d.done_detected
    assert not d.should_stop()


def test_done_detected_incrementally():
    """DONE: appears partway through streaming."""
    d = EarlyDoneDetector()
    d.feed("Let me check.\n")
    assert not d.should_stop()
    d.feed("Let me check.\nDONE: found 3 files")
    assert d.should_stop()


def test_done_after_command_block():
    """DONE: after a code block — still detected."""
    d = EarlyDoneDetector()
    d.feed("```bash\nls\n```\nDONE: listed files")
    assert d.done_detected


def test_done_detected_only_once():
    """Flag stays True, no redundant work."""
    d = EarlyDoneDetector()
    d.feed("DONE: first")
    assert d.done_detected
    d.feed("DONE: first\nDONE: second")
    assert d.done_detected  # still True, no crash


def test_partial_done_not_detected():
    """'DONE' without colon is not a signal."""
    d = EarlyDoneDetector()
    d.feed("We are DONE with the setup")
    assert not d.done_detected


def test_empty_input():
    d = EarlyDoneDetector()
    d.feed("")
    assert not d.done_detected
