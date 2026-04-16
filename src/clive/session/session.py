"""Tmux session and pane management."""

import time
import uuid

import libtmux

import logging

from output import progress
from models import PaneInfo
from prompts import load_driver_meta
from runtime import resolve_model_tier

log = logging.getLogger(__name__)

SESSION_NAME = "clive"
SOCKET_NAME = "clive"  # Dedicated tmux server socket — isolates clive from user sessions


def generate_session_id() -> str:
    """Generate a short unique session ID."""
    return uuid.uuid4().hex[:8]


def setup_session(
    tools: list[dict],
    session_name: str = SESSION_NAME,
    session_dir: str | None = None,
) -> tuple[libtmux.Session, dict[str, PaneInfo]]:
    """Create tmux session with one window+pane per tool."""
    server = libtmux.Server(socket_name=SOCKET_NAME)
    # Use session_dir suffix to avoid killing concurrent instances
    if session_dir:
        import os
        suffix = os.path.basename(session_dir)
        session_name = f"{session_name}-{suffix}"
    session = server.new_session(
        session_name=session_name,
        kill_session=True,
        attach=False,
        window_name=tools[0]["name"],
    )
    # Panes survive process exits — preserves output for debugging and
    # prevents cascading failures when SSH connections drop.
    server.cmd('set-option', '-t', session_name, 'remain-on-exit', 'on')

    panes: dict[str, PaneInfo] = {}

    for i, tool in enumerate(tools):
        if i == 0:
            window = session.active_window
            window.rename_window(tool["name"])
        else:
            window = session.new_window(window_name=tool["name"], attach=False)

        pane = window.active_pane
        is_remote = bool(tool.get("host"))

        if not is_remote:
            # Local tools: set up environment, then launch
            pane.send_keys('export PS1="[AGENT_READY] $ "', enter=True)
            pane.send_keys(
                f'printf "\\033]2;{tool["app_type"]}\\033\\\\"',
                enter=True,
            )
            if tool.get("cmd"):
                pane.send_keys(tool["cmd"], enter=True)
        else:
            # Remote tools: connect first, then set up environment on remote
            if tool.get("cmd"):
                pane.send_keys(tool["cmd"], enter=True)
            else:
                pane.send_keys(f"ssh {tool['host']}", enter=True)
            time.sleep(tool.get("connect_timeout", 3))
            pane.send_keys('export PS1="[AGENT_READY] $ "', enter=True)
            pane.send_keys(
                f'printf "\\033]2;{tool["app_type"]}\\033\\\\"',
                enter=True,
            )

        driver_meta = load_driver_meta(tool["app_type"])
        panes[tool["name"]] = PaneInfo(
            pane=pane,
            app_type=tool["app_type"],
            description=tool["description"],
            name=tool["name"],
            idle_timeout=tool.get("idle_timeout", 2.0),
            frame_nonce=tool.get("frame_nonce", ""),
            agent_model=resolve_model_tier(driver_meta.get("agent_model")),
            observation_model=resolve_model_tier(driver_meta.get("observation_model")),
        )

    # Set working directory to where clive was launched, create session dir for outputs
    import os
    cwd = os.getcwd()
    workdir = session_dir or "/tmp/clive"
    setup_markers = {}
    for name, pane_info in panes.items():
        marker = f"___SETUP_{uuid.uuid4().hex[:4]}___"
        pane_info.pane.send_keys(
            f"cd {cwd} && mkdir -p {workdir}; echo {marker}", enter=True
        )
        setup_markers[name] = marker

    # Wait for all panes to finish setup (poll all in parallel)
    pending = set(setup_markers.keys())
    start = time.time()
    poll = 0.01
    while pending and time.time() - start < 10.0:
        for name in list(pending):
            lines = panes[name].pane.cmd("capture-pane", "-p", "-J").stdout
            screen = "\n".join(lines) if lines else ""
            if setup_markers[name] in screen:
                pending.discard(name)
        if pending:
            time.sleep(poll)
            poll = min(poll * 2, 0.5)

    return session, panes, session_name


def add_pane(session: libtmux.Session, tool: dict, session_dir: str | None = None) -> PaneInfo:
    """Add a single pane to a running session. Returns PaneInfo."""
    import os, uuid
    window = session.new_window(window_name=tool["name"], attach=False)
    pane = window.active_pane
    is_remote = bool(tool.get("host"))

    if not is_remote:
        pane.send_keys('export PS1="[AGENT_READY] $ "', enter=True)
        pane.send_keys(f'printf "\\033]2;{tool["app_type"]}\\033\\\\"', enter=True)
        if tool.get("cmd"):
            pane.send_keys(tool["cmd"], enter=True)
    else:
        if tool.get("cmd"):
            pane.send_keys(tool["cmd"], enter=True)
        else:
            pane.send_keys(f"ssh {tool['host']}", enter=True)
        time.sleep(tool.get("connect_timeout", 3))
        pane.send_keys('export PS1="[AGENT_READY] $ "', enter=True)
        pane.send_keys(f'printf "\\033]2;{tool["app_type"]}\\033\\\\"', enter=True)

    cwd = os.getcwd()
    workdir = session_dir or "/tmp/clive"
    marker = f"___SETUP_{uuid.uuid4().hex[:4]}___"
    pane.send_keys(f"cd {cwd} && mkdir -p {workdir}; echo {marker}", enter=True)

    # Wait for ready
    start = time.time()
    poll = 0.01
    while time.time() - start < 5.0:
        lines = pane.cmd("capture-pane", "-p", "-J").stdout
        screen = "\n".join(lines) if lines else ""
        if marker in screen:
            break
        time.sleep(poll)
        poll = min(poll * 2, 0.3)

    driver_meta = load_driver_meta(tool["app_type"])
    info = PaneInfo(
        pane=pane,
        app_type=tool["app_type"],
        description=tool["description"],
        name=tool["name"],
        idle_timeout=tool.get("idle_timeout", 2.0),
        frame_nonce=tool.get("frame_nonce", ""),
        agent_model=resolve_model_tier(driver_meta.get("agent_model")),
        observation_model=resolve_model_tier(driver_meta.get("observation_model")),
    )
    _maybe_attach_stream(info, session_dir)
    return info


def _maybe_attach_stream(pane_info: PaneInfo, session_dir: str | None) -> None:
    """Attach a PaneStream + PaneLoop to pane_info if CLIVE_STREAMING_OBS=1.

    Creates ``{session_dir or /tmp/clive}/pipes/{pane_name}.fifo``, runs
    ``tmux pipe-pane -o 'cat > <fifo>'`` so tmux writes all pane output
    into the pipe, spins up a PaneLoop (per-pane asyncio loop on a
    background thread), and constructs a PaneStream on that loop so
    its reader task lives alongside later consumers.

    Silent fallback on any failure: pane_info.stream stays None and
    the polling observation path continues to work unchanged.
    """
    import os
    if os.environ.get("CLIVE_STREAMING_OBS") != "1":
        return

    base = session_dir or "/tmp/clive"
    fifo_dir = os.path.join(base, "pipes")
    fifo_path = os.path.join(fifo_dir, f"{pane_info.name}.fifo")

    pane_loop = None
    try:
        os.makedirs(fifo_dir, exist_ok=True)
        # Idempotent: stale FIFO from a previous non-cleaned run shouldn't
        # block a fresh attach.
        if os.path.exists(fifo_path):
            os.unlink(fifo_path)
        os.mkfifo(fifo_path)

        # Start tmux writing to the FIFO before we open the read side.
        # PaneStream uses O_NONBLOCK so open order is actually flexible,
        # but running pipe-pane first means bytes start flowing sooner.
        pane_info.pane.cmd("pipe-pane", "-o", f"cat > {fifo_path}")

        from pane_loop import PaneLoop
        from fifo_stream import PaneStream
        pane_loop = PaneLoop.start()

        # PaneStream.from_fifo_path calls asyncio.create_task, which
        # requires a running loop. Submit it to the pane loop so the
        # reader task is bound to the loop that will consume its queue.
        async def _create():
            return PaneStream.from_fifo_path(fifo_path)
        stream = pane_loop.submit(_create()).result(timeout=2.0)

        pane_info.pane_loop = pane_loop
        pane_info.stream = stream
    except Exception as e:
        log.warning(
            "stream setup failed for pane %s: %s (falling back to poll path)",
            pane_info.name, e,
        )
        if pane_loop is not None:
            try:
                pane_loop.stop()
            except Exception:
                pass
        pane_info.stream = None
        pane_info.pane_loop = None


def detach_stream(pane_info: PaneInfo) -> None:
    """Reverse of ``_maybe_attach_stream``. Safe to call if nothing attached.

    Order matters: tmux pipe-pane off first (stop writers), then close
    the PaneStream on its own loop, stop the loop, finally unlink the
    FIFO. Exceptions at each step are logged but don't halt teardown.
    """
    import os
    stream = pane_info.stream
    pane_loop = pane_info.pane_loop
    if stream is None and pane_loop is None:
        return

    # Grab fifo_path before we null anything.
    fifo_path = stream.fifo_path if stream is not None else None

    # Toggle pipe-pane off. The -o flag toggles the active pipe when
    # given no shell command, matching the state we set in attach.
    try:
        pane_info.pane.cmd("pipe-pane", "-o")
    except Exception as e:
        log.warning("pipe-pane off failed for %s: %s", pane_info.name, e)

    # Close the stream on the loop that owns its reader task.
    if (
        stream is not None
        and pane_loop is not None
        and pane_loop.thread
        and pane_loop.thread.is_alive()
    ):
        try:
            pane_loop.submit(stream.close()).result(timeout=2.0)
        except Exception as e:
            log.warning("stream.close() failed for %s: %s", pane_info.name, e)

    if pane_loop is not None:
        try:
            pane_loop.stop()
        except Exception:
            pass

    if fifo_path and os.path.exists(fifo_path):
        try:
            os.unlink(fifo_path)
        except OSError as e:
            log.warning("fifo unlink failed for %s: %s", fifo_path, e)

    pane_info.stream = None
    pane_info.pane_loop = None


def add_conversational_pane(session: libtmux.Session) -> libtmux.Pane:
    """Add a 'conversational' window to an existing tmux session.

    Named instances use this pane to listen for tasks from other clive
    instances via the local address resolution protocol.
    """
    window = session.new_window(window_name="conversational", attach=False)
    pane = window.active_pane
    pane.send_keys('export PS1="[CONVERSATIONAL_READY] $ "', enter=True)
    return pane


def check_health(panes: dict[str, PaneInfo]) -> dict[str, dict]:
    """Verify each pane shows [AGENT_READY]. Returns status dict."""
    status = {}
    for name, info in panes.items():
        lines = info.pane.cmd("capture-pane", "-p").stdout
        screen = "\n".join(lines) if lines else ""
        ready = "[AGENT_READY]" in screen
        status[name] = {
            "status": "ready" if ready else "unavailable",
            "app_type": info.app_type,
            "description": info.description,
        }
        logging.debug(f"Health: {name} [{info.app_type}] {'ready' if ready else 'unavailable'}")
    return status


def capture_pane(pane_info: PaneInfo, scrollback: int = 50) -> str:
    """Capture current screen content from a single pane.

    Uses -J to join wrapped lines (prevents long output lines from appearing
    as multiple screen lines) and -S to include recent scrollback.
    Strips leading/trailing blank lines to reduce noise and token waste.
    """
    lines = pane_info.pane.cmd(
        "capture-pane", "-p", "-J", f"-S-{scrollback}"
    ).stdout
    if not lines:
        return ""
    # Strip leading blank lines (empty scrollback above first command)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).rstrip()


def ensure_agent_pane(
    session: libtmux.Session,
    panes: dict[str, PaneInfo],
    host: str,
    config: dict,
) -> PaneInfo:
    """Lazily create an agent pane for clive@host if it doesn't exist.

    If agent-{host} already exists in panes, returns it.
    Otherwise creates a new tmux window, opens SSH, and adds to panes.
    """
    pane_name = f"agent-{host}"

    if pane_name in panes:
        return panes[pane_name]

    window = session.new_window(window_name=pane_name, attach=False)
    pane = window.active_pane

    cmd = config.get("cmd", f"ssh {host}")
    pane.send_keys(cmd, enter=True)
    time.sleep(config.get("connect_timeout", 3))

    pane_info = PaneInfo(
        pane=pane,
        app_type=config.get("app_type", "agent"),
        description=config.get("description", f"Remote clive at {host}"),
        name=pane_name,
        idle_timeout=config.get("idle_timeout", 5.0),
        frame_nonce=config.get("frame_nonce", ""),
    )
    panes[pane_name] = pane_info

    progress(f"  ✓ {pane_name} [agent] connected")
    return pane_info

