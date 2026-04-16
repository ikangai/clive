"""Tests for L2 byte-stream regex classifier."""
from byte_classifier import ByteClassifier, ByteEvent


def test_detects_red_fg():
    clf = ByteClassifier()
    events = clf.feed(b"\x1b[31mERROR\x1b[0m")
    kinds = [e.kind for e in events]
    assert "color_alert" in kinds


def test_detects_password_prompt():
    clf = ByteClassifier()
    events = clf.feed(b"Please enter password: ")
    assert any(e.kind == "password_prompt" for e in events)


def test_detects_yn_prompt():
    clf = ByteClassifier()
    events = clf.feed(b"Continue? [y/N] ")
    assert any(e.kind == "confirm_prompt" for e in events)


def test_detects_cmd_end_marker():
    clf = ByteClassifier()
    events = clf.feed(b"output line\nEXIT:0 ___DONE_abcd\n")
    assert any(e.kind == "cmd_end" for e in events)


def test_multiple_pattern_kinds_same_chunk():
    """Regression: don't let an earlier pattern's match suppress a later
    pattern's match at a lower offset."""
    clf = ByteClassifier()
    events = clf.feed(b"Traceback (most recent) \x1b[31mRED\x1b[0m")
    kinds = {e.kind for e in events}
    assert "error_keyword" in kinds
    assert "color_alert" in kinds


def test_cross_chunk_pattern():
    # "password:" split across two feeds must still match.
    clf = ByteClassifier()
    events1 = clf.feed(b"passw")
    events2 = clf.feed(b"ord: ")
    kinds = [e.kind for e in events1 + events2]
    assert "password_prompt" in kinds


def test_does_not_fire_twice_for_same_match():
    clf = ByteClassifier()
    clf.feed(b"Password: ")
    events2 = clf.feed(b"")
    assert not events2


def test_ring_buffer_bounded():
    clf = ByteClassifier()
    big = b"x" * (128 * 1024)
    clf.feed(big)
    assert len(clf._carryover) <= 128
