"""Tmux session and pane management."""

import time

import libtmux

from models import PaneInfo

SESSION_NAME = "agent"

DEFAULT_TOOLS = [
    {
        "name": "shell",
        "cmd": None,
        "app_type": "shell",
        "description": "General purpose bash shell for filesystem ops and scripting",
        "host": None,
    },
    {
        "name": "browser",
        "cmd": None,
        "app_type": "browser",
        "description": "Fetch and render web pages as plain text. Usage: lynx -dump <url>",
        "host": None,
    },
    {
        "name": "email",
        "cmd": "bash ./fetch_emails.sh",
        "app_type": "email_cli",
        "description": (
            "Fetches unread IMAP emails as plain text. "
            "To send a reply: bash ./send_reply.sh <to> <subject> <body>. "
            "To search: neomutt -e 'limit ~s keyword'"
        ),
        "host": None,
    },
    # {
    #     "name": "calendar",
    #     "cmd": "bash /opt/tools/calendar.sh",
    #     "app_type": "calendar_cli",
    #     "description": "Shows today's events and upcoming schedule",
    #     "host": None,
    # },
    # Remote shell via SSH (set host to enable remote setup):
    # {
    #     "name": "build_server",
    #     "cmd": "ssh deploy@build.example.com",
    #     "app_type": "shell",
    #     "description": "Build server — run tests, compile, check logs",
    #     "host": "deploy@build.example.com",
    # },
    # Remote agent (agent-to-agent):
    # {
    #     "name": "remote_agent",
    #     "cmd": "ssh deploy@agents.example.com 'python agent.py'",
    #     "app_type": "agent",
    #     "description": "Remote agent. Send tasks as plain text.",
    #     "host": "deploy@agents.example.com",
    #     "connect_timeout": 5,
    # },
]


def setup_session(
    tools: list[dict],
    session_name: str = SESSION_NAME,
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

    # shared working directory
    list(panes.values())[0].pane.send_keys("mkdir -p /tmp/agent", enter=True)
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
        print(f"  {indicator} {name:16} [{info.app_type}]")
    return status


def capture_pane(pane_info: PaneInfo) -> str:
    """Capture current screen content from a single pane."""
    lines = pane_info.pane.cmd("capture-pane", "-p").stdout
    return "\n".join(lines) if lines else ""


def get_meta(pane: libtmux.Pane) -> str:
    """Read pane title metadata."""
    try:
        return pane.cmd("display-message", "-p", "#T").stdout[0]
    except Exception:
        return "unknown"
