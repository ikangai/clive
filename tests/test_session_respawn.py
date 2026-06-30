"""Tests for self-healing of DEAD tmux panes in session.py.

session sets ``remain-on-exit on`` (session.py), so a pane whose process or
shell exits is held in a DEAD state rather than closed — its output survives
for debugging, but it can never run another command, so every subtask routed
there fails or burns max_turns. ``respawn_dead_panes`` gives each DEAD pane one
recovery attempt: read ``#{pane_dead}``, and for a dead pane issue
``respawn-pane -k`` then re-install the AGENT_READY prompt + pager-safe env so
the pane looks identical to a freshly set-up one. ``check_health`` calls it
before reporting, so a crashed pane self-heals once instead of being reported
permanently unavailable.

Pure-unit: panes are tiny fakes (the fake-pane pattern from
``test_session_capture_retry.py``). No real tmux.
"""
import session
from session import respawn_dead_panes, check_health


class _Result:
    """Stand-in for a libtmux cmd result: only ``.stdout`` is read."""

    def __init__(self, lines):
        self.stdout = lines


class _FakePane:
    """Fake tmux pane that models the DEAD/alive state machine.

    ``cmd`` answers the three commands respawn touches: ``display-message``
    reports ``#{pane_dead}`` (1 when dead), ``respawn-pane`` restarts the
    shell in place (clears DEAD), and ``capture-pane`` shows the AGENT_READY
    prompt once the shell is alive. ``send_keys`` records the re-setup lines.
    """

    def __init__(self, dead: bool):
        self.dead = dead
        self.cmds: list[tuple] = []
        self.sent: list[str] = []
        # Unified, ordered log of every cmd/send_keys so a test can assert
        # ordering *across* the two (e.g. a launch_cmd re-sent after respawn).
        self.events: list[tuple] = []
        self.respawned = False

    def cmd(self, *args):
        self.cmds.append(args)
        self.events.append(("cmd",) + args)
        head = args[0] if args else ""
        if head == "display-message":
            return _Result(["1" if self.dead else "0"])
        if head == "respawn-pane":
            self.respawned = True
            self.dead = False  # shell restarted in place
            return _Result([])
        if head == "capture-pane":
            # A live (or freshly respawned) shell shows the AGENT_READY prompt;
            # a still-DEAD pane shows only its terminated last screen.
            ready = "[AGENT_READY] ec=0 $" if not self.dead else "<process exited>"
            return _Result([ready])
        return _Result([])

    def send_keys(self, text, enter=False):
        self.sent.append(text)
        self.events.append(("send", text))


class _FakeStream:
    """Minimal PaneStream stand-in: ``detach_stream`` only reads ``.fifo_path``.

    ``fifo_path=None`` makes the ``os.path.exists`` unlink branch a no-op so the
    fake needs no real FIFO on disk.
    """

    def __init__(self):
        self.fifo_path = None


class _FakePaneLoop:
    """Minimal PaneLoop stand-in for ``detach_stream``.

    ``thread=None`` short-circuits the close-on-loop block (no asyncio needed);
    ``stop`` is still called, so the test can confirm the loop was torn down.
    """

    def __init__(self):
        self.thread = None
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakePaneInfo:
    # ``launch_cmd`` mirrors the real PaneInfo dataclass default ('').
    # ``stream``/``pane_loop`` mirror the optional streaming-observation fields
    # (None unless CLIVE_STREAMING_OBS attached a PaneStream); a respawn must
    # tear a now-stale one down via detach_stream.
    def __init__(self, pane, app_type="shell", description="d", name="shell",
                 launch_cmd="", stream=None, pane_loop=None):
        self.pane = pane
        self.app_type = app_type
        self.description = description
        self.name = name
        self.launch_cmd = launch_cmd
        self.stream = stream
        self.pane_loop = pane_loop


def test_respawn_dead_pane_restarts_and_resets_agent_ready():
    """A pane reporting pane_dead=1 is respawned and gets AGENT_READY re-setup."""
    pane = _FakePane(dead=True)
    info = _FakePaneInfo(pane, name="shell")

    respawned = respawn_dead_panes({"shell": info})

    # The dead pane was respawned with -k and reported back.
    assert respawned == ["shell"]
    assert pane.respawned is True
    assert ("respawn-pane", "-k") in pane.cmds
    # AGENT_READY prompt + pager-safe env were re-installed, in that order.
    assert pane.sent == [
        session.agent_ready_prompt_setup(),
        session.pager_safe_env_setup(),
    ]


def test_respawn_leaves_live_pane_untouched():
    """A pane reporting pane_dead=0 is never respawned and gets no re-setup."""
    pane = _FakePane(dead=False)
    info = _FakePaneInfo(pane, name="shell")

    respawned = respawn_dead_panes({"shell": info})

    assert respawned == []
    assert pane.respawned is False
    assert ("respawn-pane", "-k") not in pane.cmds
    assert pane.sent == []


def test_check_health_recovers_dead_pane_to_ready():
    """check_health respawns a DEAD pane once, then reports it ready."""
    pane = _FakePane(dead=True)
    info = _FakePaneInfo(pane, name="shell")

    status = check_health({"shell": info})

    # Recovery happened exactly once and the pane is now reported ready
    # instead of unavailable.
    assert pane.cmds.count(("respawn-pane", "-k")) == 1
    assert status["shell"]["status"] == "ready"


def test_check_health_live_pane_unchanged():
    """A live pane is reported ready without any respawn side effects."""
    pane = _FakePane(dead=False)
    info = _FakePaneInfo(pane, name="shell")

    status = check_health({"shell": info})

    assert pane.respawned is False
    assert pane.sent == []
    assert status["shell"]["status"] == "ready"


def test_respawn_replays_launch_cmd_after_respawn():
    """A DEAD pane carrying a launch_cmd has it re-sent after respawn-pane -k.

    A pane originally launched with a non-shell command (``ssh remotehost`` for
    a REMOTE pane, an app/REPL ``cmd`` for a tool pane) must come back as that
    same thing — not a bare local shell — or every subtask routed there runs
    remote/app-intended commands on the local box. ``respawn-pane -k`` only
    restarts the *shell*, so the stored launch command is replayed afterwards.
    """
    pane = _FakePane(dead=True)
    info = _FakePaneInfo(pane, name="remote", launch_cmd="ssh remotehost")

    respawned = respawn_dead_panes({"remote": info})

    assert respawned == ["remote"]
    # The launch command was re-sent...
    assert "ssh remotehost" in pane.sent
    # ...as the last thing, i.e. after the prompt + pager-safe env re-install.
    assert pane.sent == [
        session.agent_ready_prompt_setup(),
        session.pager_safe_env_setup(),
        "ssh remotehost",
    ]
    # ...and strictly *after* the respawn-pane -k restarted the shell, so the
    # ssh reconnect runs on a live shell rather than the dead one.
    respawn_idx = pane.events.index(("cmd", "respawn-pane", "-k"))
    replay_idx = pane.events.index(("send", "ssh remotehost"))
    assert respawn_idx < replay_idx


def test_respawn_empty_launch_cmd_sends_no_extra_command():
    """A DEAD pane with launch_cmd='' (a plain local shell) gets no extra send.

    The empty-string default means the ``if info.launch_cmd:`` guard is False,
    so a bare local shell pane is re-set-up exactly as before — only the
    AGENT_READY prompt + pager-safe env, no spurious trailing command.
    """
    pane = _FakePane(dead=True)
    info = _FakePaneInfo(pane, name="shell", launch_cmd="")

    respawned = respawn_dead_panes({"shell": info})

    assert respawned == ["shell"]
    assert pane.sent == [
        session.agent_ready_prompt_setup(),
        session.pager_safe_env_setup(),
    ]


def test_respawn_detaches_stale_stream():
    """A respawned DEAD pane has its now-stale observation stream torn down.

    ``respawn-pane -k`` starts a new shell process, so the old PaneStream (bound
    to the killed process's pipe-pane->FIFO) is dead. Left attached, the
    default-on streaming observation path keeps consuming a dead stream and
    observes nothing -> burns max_turns. respawn_dead_panes must call
    ``detach_stream`` so stream/pane_loop are nulled and observation falls back
    cleanly to the working poll path.
    """
    pane = _FakePane(dead=True)
    stream = _FakeStream()
    pane_loop = _FakePaneLoop()
    info = _FakePaneInfo(pane, name="shell", stream=stream, pane_loop=pane_loop)

    respawned = respawn_dead_panes({"shell": info})

    assert respawned == ["shell"]
    # The stale stream + loop were detached (nulled) and the loop stopped.
    assert info.stream is None
    assert info.pane_loop is None
    assert pane_loop.stopped is True


def test_respawn_leaves_live_pane_stream_untouched():
    """A live pane is never respawned, so its observation stream is preserved.

    detach_stream must run only on the recovery path; a healthy pane keeps its
    working stream/pane_loop so streaming observation continues uninterrupted.
    """
    pane = _FakePane(dead=False)
    stream = _FakeStream()
    pane_loop = _FakePaneLoop()
    info = _FakePaneInfo(pane, name="shell", stream=stream, pane_loop=pane_loop)

    respawned = respawn_dead_panes({"shell": info})

    assert respawned == []
    # No respawn -> stream/pane_loop are left exactly as they were.
    assert info.stream is stream
    assert info.pane_loop is pane_loop
    assert pane_loop.stopped is False
