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


def test_done_inside_closed_fence_ignored():
    """A DONE: line inside a finished code block must not abort streaming."""
    d = EarlyDoneDetector()
    d.feed("```bash\necho 'DONE: noise'\n```")
    assert not d.done_detected
    assert not d.should_stop()


def test_done_inside_open_fence_ignored_midstream():
    """Mid-stream, an unclosed fence whose body starts with DONE: must not abort.

    Otherwise streaming aborts mid-command and the command is truncated.
    """
    d = EarlyDoneDetector()
    d.feed("I'll write a marker.\n```bash\nDONE: leftover line")
    assert not d.done_detected
    assert not d.should_stop()


def test_done_in_heredoc_body_ignored():
    d = EarlyDoneDetector()
    d.feed("```bash\ncat <<EOF\nDONE: data not signal\nEOF\n```")
    assert not d.done_detected


def test_genuine_done_after_closed_fence_still_detected():
    """Real top-level DONE: outside the fence still aborts streaming."""
    d = EarlyDoneDetector()
    d.feed("```bash\necho 'DONE: noise'\n```\nDONE: real summary")
    assert d.done_detected
    assert d.should_stop()
