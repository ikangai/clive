"""Tmux session and pane management."""

import time
import uuid

import libtmux

from output import progress
from models import PaneInfo

SESSION_NAME = "clive"


def generate_session_id() -> str:
    """Generate a short unique session ID."""
    return uuid.uuid4().hex[:8]


def setup_session(
    tools: list[dict],
    session_name: str = SESSION_NAME,
    session_dir: str | None = None,
) -> tuple[libtmux.Session, dict[str, PaneInfo]]:
    """Create tmux session with one window+pane per tool."""
    server = libtmux.Server()
    session = server.new_session(
        session_name=session_name,
        kill_session=True,
        attach=False,
        window_name=tools[0]["name"],
    )

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

        panes[tool["name"]] = PaneInfo(
            pane=pane,
            app_type=tool["app_type"],
            description=tool["description"],
            name=tool["name"],
            idle_timeout=tool.get("idle_timeout", 2.0),
        )

    # session-scoped working directory
    workdir = session_dir or "/tmp/clive"
    list(panes.values())[0].pane.send_keys(f"mkdir -p {workdir}", enter=True)
    time.sleep(1.5)

    return session, panes


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
        indicator = "✓" if ready else "✗"
        progress(f"  {indicator} {name:16} [{info.app_type}]")
    return status


def capture_pane(pane_info: PaneInfo, scrollback: int = 100) -> str:
    """Capture current screen content from a single pane.

    Uses -J to join wrapped lines (prevents long output lines from appearing
    as multiple screen lines) and -S to include recent scrollback.
    """
    lines = pane_info.pane.cmd(
        "capture-pane", "-p", "-J", f"-S-{scrollback}"
    ).stdout
    return "\n".join(lines).rstrip() if lines else ""


def get_meta(pane: libtmux.Pane) -> str:
    """Read pane title metadata."""
    try:
        return pane.cmd("display-message", "-p", "#T").stdout[0]
    except Exception:
        return "unknown"
