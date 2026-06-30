"""Tests that startup wait-loop capture-pane reads survive a transient hiccup.

``setup_session`` and ``add_pane`` poll ``capture-pane -p -J`` inside their
marker-wait loops; a single libtmux/subprocess glitch on one of those reads
must not abort startup before any subtask runs. These tests mirror the
``capture_pane``/``check_health`` retry tests in
``test_session_capture_retry.py``: no real tmux — the fake pane raises a
transient error on its first capture-pane read, then replays the lines it was
sent so the setup marker echo is found and the wait loop completes normally.
"""
import session


class _Result:
    """Stand-in for a libtmux cmd result: only ``.stdout`` is read."""

    def __init__(self, lines):
        self.stdout = lines


class _FlakyMarkerPane:
    """Records ``send_keys``; replays them on ``capture-pane`` so the setup
    marker echo is found — but raises ``exc`` on the first ``fail_times``
    capture-pane reads to exercise the startup retry guard."""

    def __init__(self, fail_times=1, exc=OSError):
        self.sent = []
        self.fail_times = fail_times
        self.exc = exc
        self.capture_calls = 0

    def send_keys(self, cmd, enter=True):
        self.sent.append(cmd)

    def cmd(self, *args):
        # Only the capture-pane reads route through _pane_cmd_with_retry.
        if args[:1] == ("capture-pane",):
            self.capture_calls += 1
            if self.capture_calls <= self.fail_times:
                raise self.exc("transient tmux hiccup")
        return _Result(list(self.sent))


class _FakeWindow:
    def __init__(self):
        self.active_pane = _FlakyMarkerPane()

    def rename_window(self, name):
        pass


class _FakeSession:
    def __init__(self):
        self.active_window = _FakeWindow()
        self.windows = [self.active_window]

    def new_window(self, window_name=None, attach=False):
        w = _FakeWindow()
        self.windows.append(w)
        return w


class _FakeServer:
    def __init__(self, socket_name=None):
        self.session = _FakeSession()

    def new_session(self, **kwargs):
        return self.session

    def cmd(self, *args):
        return _Result([])


def _local(name):
    return {"name": name, "app_type": "shell", "description": "d"}


def _no_sleep(_delay):
    """sleep_fn that returns instantly (keeps backoff out of the test)."""


def _patch_default_sleep(monkeypatch):
    """Make the helper's bound ``sleep_fn`` default a no-op (instant tests).

    ``sleep_fn=time.sleep`` is captured as a keyword default at definition
    time, so the wait loops (which call the helper without passing sleep_fn)
    use ``__kwdefaults__``; patch that to avoid real backoff delays.
    """
    monkeypatch.setitem(
        session._pane_cmd_with_retry.__kwdefaults__, "sleep_fn", _no_sleep
    )


def test_setup_session_survives_transient_capture_error(monkeypatch, tmp_path):
    """A transient hiccup on the first startup capture-pane read is retried,
    and setup_session still completes and returns its panes."""
    _patch_default_sleep(monkeypatch)
    monkeypatch.setattr(session.libtmux, "Server", _FakeServer)
    monkeypatch.setattr(session.time, "sleep", lambda *_a, **_k: None)

    _sess, panes, _name = session.setup_session(
        [_local("loc")], session_dir=str(tmp_path)
    )

    assert "loc" in panes
    pane = panes["loc"].pane
    # The first read raised; the retry read found the marker and the wait loop
    # completed — so there must be at least two capture-pane calls.
    assert pane.capture_calls >= 2


def test_add_pane_survives_transient_capture_error(monkeypatch, tmp_path):
    """A transient hiccup on the first startup capture-pane read is retried,
    and add_pane still completes and returns its PaneInfo."""
    _patch_default_sleep(monkeypatch)
    monkeypatch.setattr(session.time, "sleep", lambda *_a, **_k: None)
    # Keep the test scoped to the wait loop: skip the optional streaming attach.
    monkeypatch.setenv("CLIVE_STREAMING_OBS", "0")

    sess = _FakeSession()
    info = session.add_pane(sess, _local("explore"), session_dir=str(tmp_path))

    assert info is not None
    assert info.name == "explore"
    assert info.pane.capture_calls >= 2
