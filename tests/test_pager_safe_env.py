"""Tests for the deterministic pager/editor env backstop at pane setup.

Pager avoidance used to be advisory only (drivers/default.md + llm/prompts.py
tell the model to pipe through ``| cat`` / use ``git --no-pager``). Any common
pager-invoking command (git log/diff, man, systemctl status, docker logs) would
otherwise open an interactive pager and wedge the pane. This module pins a pure
helper, ``ps1_exit.pager_safe_env_setup``, that returns the export string sent at
pane setup, and checks it is wired into every setup_session/add_pane send_keys
site (covering interactive, script, AND exploration panes).
"""
import pytest


# ─── the pure helper ─────────────────────────────────────────────────────

def test_pager_safe_env_exports_all_vars():
    from ps1_exit import pager_safe_env_setup
    setup = pager_safe_env_setup()
    # Every pager/editor/prompt knob must be pinned to a non-interactive value.
    assert "PAGER=cat" in setup
    assert "GIT_PAGER=cat" in setup
    assert "MANPAGER=cat" in setup
    assert "EDITOR=true" in setup
    assert "GIT_TERMINAL_PROMPT=0" in setup
    assert "LESS=-FRX" in setup


def test_pager_safe_env_is_a_single_export():
    from ps1_exit import pager_safe_env_setup
    setup = pager_safe_env_setup()
    # One deterministic, side-effect-free export line — no shell branching.
    assert setup.startswith("export ")
    assert "\n" not in setup
    # Exact contract string (the wiring sends this verbatim).
    assert setup == (
        "export PAGER=cat GIT_PAGER=cat MANPAGER=cat "
        "EDITOR=true GIT_TERMINAL_PROMPT=0 LESS=-FRX"
    )


def test_pager_safe_env_is_deterministic():
    from ps1_exit import pager_safe_env_setup
    assert pager_safe_env_setup() == pager_safe_env_setup()


# ─── wiring into setup_session / add_pane (no real tmux) ──────────────────

class _Result:
    def __init__(self, lines):
        self.stdout = lines


class _FakePane:
    """Records send_keys; capture-pane replays sent lines so the setup marker
    echo is found immediately and polling loops terminate at once."""

    def __init__(self):
        self.sent = []

    def send_keys(self, cmd, enter=True):
        self.sent.append(cmd)

    def cmd(self, *args):
        return _Result(list(self.sent))


class _FakeWindow:
    def __init__(self):
        self.active_pane = _FakePane()

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


def _remote(name):
    return {
        "name": name,
        "app_type": "shell",
        "description": "d",
        "host": "remote-box",
        "connect_timeout": 0,
    }


def test_setup_session_sends_pager_env_local_and_remote(monkeypatch, tmp_path):
    import session
    from ps1_exit import pager_safe_env_setup, agent_ready_prompt_setup

    monkeypatch.setattr(session.libtmux, "Server", _FakeServer)
    monkeypatch.setattr(session.time, "sleep", lambda *_a, **_k: None)

    tools = [_local("loc"), _remote("rem")]
    _sess, panes, _name = session.setup_session(tools, session_dir=str(tmp_path))

    pager = pager_safe_env_setup()
    for name in ("loc", "rem"):
        assert pager in panes[name].pane.sent, f"pager env not sent to {name}"

    # Local: pager env comes right after the PS1 setup, before the tool cmd.
    loc_sent = panes["loc"].pane.sent
    assert loc_sent.index(agent_ready_prompt_setup()) < loc_sent.index(pager)


def test_add_pane_sends_pager_env_local(monkeypatch, tmp_path):
    import session
    from ps1_exit import pager_safe_env_setup

    monkeypatch.setattr(session.time, "sleep", lambda *_a, **_k: None)
    sess = _FakeSession()
    info = session.add_pane(sess, _local("explore"), session_dir=str(tmp_path))
    assert pager_safe_env_setup() in info.pane.sent


def test_add_pane_sends_pager_env_remote(monkeypatch, tmp_path):
    import session
    from ps1_exit import pager_safe_env_setup, agent_ready_prompt_setup

    monkeypatch.setattr(session.time, "sleep", lambda *_a, **_k: None)
    sess = _FakeSession()
    info = session.add_pane(sess, _remote("rem"), session_dir=str(tmp_path))

    sent = info.pane.sent
    pager = pager_safe_env_setup()
    assert pager in sent
    # Remote: pager env is sent after the PS1 setup (which is after the ssh).
    assert sent.index(agent_ready_prompt_setup()) < sent.index(pager)
