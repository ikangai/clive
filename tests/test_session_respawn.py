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
        self.respawned = False

    def cmd(self, *args):
        self.cmds.append(args)
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


class _FakePaneInfo:
    def __init__(self, pane, app_type="shell", description="d", name="shell"):
        self.pane = pane
        self.app_type = app_type
        self.description = description
        self.name = name


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
