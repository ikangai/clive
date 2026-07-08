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


def test_cmd_end_event_carries_full_marker():
    # The cmd_end event's match_bytes must carry the WHOLE marker, not just
    # the common prefix. await_ready_events (completion.py) confirms a
    # completion via `marker.encode() in evt.match_bytes`, where marker is
    # wrap_command's full ___DONE_{subtask_id}_{nonce}___ token. A pattern
    # that captured only the `EXIT:\d+ ___DONE_` prefix would make that check
    # always fail (and would collide across subtasks, since the prefix is
    # common to all of them).
    from completion import wrap_command

    _wrapped, marker = wrap_command("cmd", "sub42")
    # The rendered output is what the echo prints once `$?` is expanded:
    rendered = f"build output\nEXIT:0 {marker}\n".encode()

    clf = ByteClassifier()
    events = clf.feed(rendered)
    cmd_end = [e for e in events if e.kind == "cmd_end"]
    assert cmd_end, "expected a cmd_end event for the rendered marker"
    assert marker.encode() in cmd_end[0].match_bytes


# --- event-path intervention parity (gh#40 follow-up) --------------------
# Bring three poll-path INTERVENTION_PATTERNS kinds to the byte stream so a
# pane that hits them during streaming observation is surfaced, not stuck.


def test_detects_overwrite_prompt():
    clf = ByteClassifier()
    events = clf.feed(b"File exists. Overwrite? ")
    assert any(e.kind == "overwrite_prompt" for e in events)


def test_detects_continue_prompt():
    clf = ByteClassifier()
    events = clf.feed(b"Press any key to continue")
    assert any(e.kind == "continue_prompt" for e in events)


def test_detects_disk_error():
    clf = ByteClassifier()
    events = clf.feed(b"write failed: No space left on device")
    assert any(e.kind == "disk_error" for e in events)


def test_detects_sudo_password_prompt():
    # sudo's prompt puts the colon after the username, not "password",
    # so the bare `[Pp]assword\s*:` rule misses it.
    clf = ByteClassifier()
    events = clf.feed(b"[sudo] password for martin: ")
    assert any(e.kind == "password_prompt" for e in events)


def test_detects_ssh_passphrase_prompt():
    # ssh key unlock has no "password" token at all.
    clf = ByteClassifier()
    events = clf.feed(b"Enter passphrase for key '/home/u/.ssh/id_ed25519': ")
    assert any(e.kind == "password_prompt" for e in events)


# --- pager footer + (yes/no) confirm parity (gh#40 follow-up) ------------
# The poll path (completion.py INTERVENTION_PATTERNS) catches pager footers
# (--More--, (END)) and the (yes/no) confirm form. Both are clean literal
# byte substrings, so mirror them onto the default-on byte path. ('lines
# N-N' stays poll-only: on the always-on byte path it would false-positive
# on normal output like 'lines 1-24'.)


def test_detects_pager_more():
    # `more` footer leaves the command wedged on keystrokes.
    clf = ByteClassifier()
    events = clf.feed(b"line one\nline two\n--More--(40%)")
    assert any(e.kind == "pager_prompt" for e in events)


def test_detects_pager_end():
    # `less` at end-of-file shows "(END)".
    clf = ByteClassifier()
    events = clf.feed(b"log a\nlog b\n(END)")
    assert any(e.kind == "pager_prompt" for e in events)


def test_detects_yesno_confirm():
    # The "(yes/no)" confirm form (e.g. ssh host-key prompt) is caught by
    # the poll path but was missing from the byte confirm pattern.
    clf = ByteClassifier()
    events = clf.feed(b"Continue? (yes/no) ")
    assert any(e.kind == "confirm_prompt" for e in events)


def test_pager_kind_maps_to_pager_intervention():
    # The new byte kind routes to the poll path's intervention type.
    from completion import _INTERVENTION_KIND_MAP
    assert _INTERVENTION_KIND_MAP["pager_prompt"] == "pager_prompt"


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
