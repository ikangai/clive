# server/discovery.py
"""Session discovery and collision avoidance for multiple clive instances."""

import logging
import uuid

log = logging.getLogger(__name__)

SOCKET_NAME = "clive"


def discover_sessions() -> list[dict]:
    """Discover all running clive tmux sessions.

    Returns list of dicts with keys: name, panes, created_at.
    Returns empty list if tmux server is not running or libtmux is unavailable.
    """
    try:
        import libtmux
        server = libtmux.Server(socket_name=SOCKET_NAME)
        sessions = []
        for session in server.sessions:
            if not session.name.startswith("clive"):
                continue
            panes = []
            for window in session.windows:
                for pane in window.panes:
                    panes.append({
                        "id": pane.pane_id,
                        "name": window.name,
                    })
            sessions.append({
                "name": session.name,
                "panes": panes,
                "created_at": session.created or "",
            })
        return sessions
    except Exception as e:
        log.debug("Could not discover tmux sessions: %s", e)
        return []


def generate_unique_session_name(prefix: str = "clive") -> str:
    """Generate a unique session name that won't collide with existing sessions."""
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}-{suffix}"


def check_pane_conflict(session_name: str, pane_name: str) -> bool:
    """Check if a pane name already exists in a clive session.

    Returns True if conflict exists.
    """
    try:
        import libtmux
        server = libtmux.Server(socket_name=SOCKET_NAME)
        for session in server.sessions:
            if session.name == session_name:
                for window in session.windows:
                    if window.name == pane_name:
                        return True
    except Exception:
        pass
    return False


def format_instances(sessions: list[dict]) -> str:
    """Format discovered sessions for human display."""
    if not sessions:
        return "No running clive instances found."
    lines = []
    for s in sessions:
        pane_names = [p["name"] for p in s.get("panes", [])]
        lines.append(f"  {s['name']}: {len(pane_names)} panes [{', '.join(pane_names)}]")
    return f"Running clive instances ({len(sessions)}):\n" + "\n".join(lines)
