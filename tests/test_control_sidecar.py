"""Tests for the tmux control-mode sidecar (gh#12).

The parser and subscription plumbing are pure-python and tested here
directly. The real `tmux -C` attach path is covered by an opt-in slow
test at the bottom (`-m slow`) that drives an actual tmux server on the
clive test socket.
"""
import queue
import threading
import time

import pytest

from control_sidecar import (
    ControlEvent,
    ControlSidecar,
    parse_control_line,
    unescape_output,
)


# ---------------------------------------------------------------------------
# parse_control_line
# ---------------------------------------------------------------------------

def test_parse_output_event():
    ev = parse_control_line(r"%output %42 hello world")
    assert ev == ControlEvent(kind="output", pane_id="%42", data="hello world")


def test_parse_output_unescapes_octal():
    ev = parse_control_line(r"%output %3 a\015\012b")
    assert ev.data == "a\r\nb"


def test_parse_window_close():
    ev = parse_control_line("%window-close @7")
    assert ev.kind == "window-close"
    assert ev.data == "@7"


def test_parse_session_changed():
    ev = parse_control_line("%session-changed $1 clive")
    assert ev.kind == "session-changed"
    assert ev.data == "$1 clive"


def test_parse_exit():
    ev = parse_control_line("%exit")
    assert ev.kind == "exit"


def test_parse_begin_end_blocks_are_ignored():
    assert parse_control_line("%begin 1234567890 1 0") is None
    assert parse_control_line("%end 1234567890 1 0") is None
    assert parse_control_line("%error 1234567890 1 0") is None


def test_parse_non_notification_is_ignored():
    # Command output between %begin/%end is not a notification
    assert parse_control_line("some command output") is None
    assert parse_control_line("") is None


def test_parse_unknown_notification_passes_through_as_raw():
    ev = parse_control_line("%pane-mode-changed %5")
    assert ev.kind == "raw"
    assert ev.data == "%pane-mode-changed %5"


def test_unescape_output_handles_backslash():
    assert unescape_output(r"a\\b") == "a\\b"
    assert unescape_output(r"\033[1m") == "\x1b[1m"
    assert unescape_output("plain") == "plain"


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------

def _feed(sidecar, lines):
    for line in lines:
        sidecar._dispatch_line(line)


def test_subscribe_pane_receives_only_its_output():
    sc = ControlSidecar(session_name="t")
    q5 = sc.subscribe("%5")
    _feed(sc, [r"%output %5 for-five", r"%output %6 for-six"])
    assert q5.get_nowait().data == "for-five"
    with pytest.raises(queue.Empty):
        q5.get_nowait()


def test_on_any_callback_sees_all_events():
    sc = ControlSidecar(session_name="t")
    seen = []
    sc.on_any(lambda ev: seen.append(ev.kind))
    _feed(sc, [r"%output %5 x", "%window-close @1", "%exit"])
    assert seen == ["output", "window-close", "exit"]


def test_wake_event_set_on_output():
    sc = ControlSidecar(session_name="t")
    wake = threading.Event()
    sc.wake_on_output(wake)
    assert not wake.is_set()
    _feed(sc, [r"%output %9 ping"])
    assert wake.is_set()


def test_callback_exception_does_not_break_dispatch():
    sc = ControlSidecar(session_name="t")
    seen = []
    sc.on_any(lambda ev: 1 / 0)
    sc.on_any(lambda ev: seen.append(ev.kind))
    _feed(sc, [r"%output %1 x"])
    assert seen == ["output"]


# ---------------------------------------------------------------------------
# live tmux integration (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_live_sidecar_observes_pane_output():
    import libtmux
    import uuid
    from session import SOCKET_NAME

    name = f"sidecar_test_{uuid.uuid4().hex[:6]}"
    server = libtmux.Server(socket_name=SOCKET_NAME)
    session = server.new_session(session_name=name, kill_session=True,
                                 attach=False)
    try:
        sc = ControlSidecar(session_name=name, socket_name=SOCKET_NAME)
        events = []
        sc.on_any(lambda ev: events.append(ev))
        sc.start()
        time.sleep(0.5)
        session.active_window.active_pane.send_keys(
            "echo sidecar-live-marker", enter=True
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(
                ev.kind == "output" and "sidecar-live-marker" in ev.data
                for ev in events
            ):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"no output event observed; got {events[:10]}")
        sc.stop()
    finally:
        try:
            session.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# dag_scheduler wiring (CLIVE_CONTROL_SIDECAR flag)
# ---------------------------------------------------------------------------

class _FakePane:
    class _S:
        name = "fake_session"
    class _P:
        session = None
    def __init__(self):
        self.pane = self._P()
        self.pane.session = self._S()


def test_scheduler_sidecar_off_by_default(monkeypatch):
    import executor  # noqa: F401  (resolves the circular-import topology)
    from dag_scheduler import _maybe_start_sidecar
    monkeypatch.delenv("CLIVE_CONTROL_SIDECAR", raising=False)
    assert _maybe_start_sidecar({"p": _FakePane()}, threading.Event()) is None


def test_scheduler_sidecar_started_when_flagged(monkeypatch):
    import executor  # noqa: F401
    import observation.control_sidecar as cs_mod
    from dag_scheduler import _maybe_start_sidecar

    calls = {}

    class StubSidecar:
        def __init__(self, session_name, socket_name=None):
            calls["session"] = session_name
        def wake_on_output(self, ev):
            calls["wake"] = ev
        def start(self):
            calls["started"] = True
        def stop(self):
            calls["stopped"] = True

    monkeypatch.setenv("CLIVE_CONTROL_SIDECAR", "1")
    monkeypatch.setattr(cs_mod, "ControlSidecar", StubSidecar)
    wake = threading.Event()
    sc = _maybe_start_sidecar({"p": _FakePane()}, wake)
    assert calls == {"session": "fake_session", "wake": wake, "started": True}
    sc.stop()
    assert calls["stopped"] is True


def test_scheduler_sidecar_failure_falls_back_to_none(monkeypatch):
    import executor  # noqa: F401
    import observation.control_sidecar as cs_mod
    from dag_scheduler import _maybe_start_sidecar

    class BoomSidecar:
        def __init__(self, *a, **k):
            raise RuntimeError("no tmux")

    monkeypatch.setenv("CLIVE_CONTROL_SIDECAR", "1")
    monkeypatch.setattr(cs_mod, "ControlSidecar", BoomSidecar)
    assert _maybe_start_sidecar({"p": _FakePane()}, threading.Event()) is None
