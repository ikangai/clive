"""Persistent chat sessions for clive.

Each chat session is a JSON file at ``~/.clive/sessions/{id}.json`` holding
id, timestamps, optional title, and the transcript of user tasks / summaries
produced while the session was active.

Mirrors ``registry.py``'s dir-injection pattern so tests can point at
``tmp_path`` instead of the user's real home directory.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

DEFAULT_SESSIONS_DIR = Path.home() / ".clive" / "sessions"


def _dir(sessions_dir: Path | None) -> Path:
    d = sessions_dir or DEFAULT_SESSIONS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(sid: str, sessions_dir: Path | None) -> Path:
    return _dir(sessions_dir) / f"{sid}.json"


def new(title: str | None = None, sessions_dir: Path | None = None) -> str:
    """Create a new session file and return its id."""
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    entry = {
        "id": sid,
        "title": title or "",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "tasks": [],
    }
    _path(sid, sessions_dir).write_text(json.dumps(entry, indent=2))
    return sid


def _infer_title(task: str, max_len: int = 60) -> str:
    """Strip and truncate a task string into a human-readable title."""
    t = " ".join(task.strip().split())
    if len(t) > max_len:
        return t[: max_len - 1].rstrip() + "\u2026"
    return t


def complete_last_task(sid: str, summary: str | None = None,
                       status: str = "done",
                       sessions_dir: Path | None = None) -> bool:
    """Mark the most recent task on a session as completed.

    Typical flow: ``record_task(sid, task)`` before executing, then
    ``complete_last_task(sid, summary=..., status="done"|"failed")`` after.
    Returns False if the session doesn't exist or has no tasks.
    """
    data = get(sid, sessions_dir)
    if data is None:
        return False
    tasks = data.get("tasks") or []
    if not tasks:
        return False
    last = tasks[-1]
    if summary is not None:
        last["summary"] = summary
    last["status"] = status
    last["completed_at"] = time.time()
    data["updated_at"] = time.time()
    _path(sid, sessions_dir).write_text(json.dumps(data, indent=2))
    return True


def record_task(sid: str, task: str, summary: str | None = None,
                status: str = "pending",
                sessions_dir: Path | None = None) -> bool:
    """Record a user task against a session.

    Adds a row to ``tasks`` with {task, summary, status, started_at}. If the
    session has no title yet, auto-infers one from the first task. Returns
    True on success, False if the session doesn't exist.
    """
    data = get(sid, sessions_dir)
    if data is None:
        return False
    data.setdefault("tasks", []).append({
        "task": task,
        "summary": summary,
        "status": status,
        "started_at": time.time(),
    })
    if not data.get("title"):
        data["title"] = _infer_title(task)
    data["updated_at"] = time.time()
    _path(sid, sessions_dir).write_text(json.dumps(data, indent=2))
    return True


def append_message(sid: str, role: str, content: str,
                   sessions_dir: Path | None = None) -> bool:
    """Append a message to a session's transcript. Returns True if appended."""
    data = get(sid, sessions_dir)
    if data is None:
        return False
    data.setdefault("messages", []).append({
        "role": role,
        "content": content,
        "ts": time.time(),
    })
    data["updated_at"] = time.time()
    _path(sid, sessions_dir).write_text(json.dumps(data, indent=2))
    return True


def get(sid: str, sessions_dir: Path | None = None) -> dict | None:
    p = _path(sid, sessions_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def list_sessions(sessions_dir: Path | None = None) -> list[dict]:
    d = _dir(sessions_dir)
    result = []
    for f in sorted(d.glob("*.json")):
        try:
            result.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return result


def list_sorted(sessions_dir: Path | None = None) -> list[dict]:
    """Return sessions sorted by ``updated_at`` descending (most recent first)."""
    return sorted(
        list_sessions(sessions_dir),
        key=lambda s: s.get("updated_at", 0),
        reverse=True,
    )


def most_recent(sessions_dir: Path | None = None) -> dict | None:
    """Return the most recently updated session, or None if no sessions exist."""
    sorted_list = list_sorted(sessions_dir)
    return sorted_list[0] if sorted_list else None


def build_recap_text(session: dict, last_n: int = 3) -> str:
    """Render a compact recap of the last N tasks of a session.

    Used when resuming a chat session: the recap is prepended to the next
    planner call so the LLM has context about prior user intent and outcomes.
    Returns an empty string if the session has no tasks (nothing to recap).
    """
    tasks = session.get("tasks") or []
    if not tasks:
        return ""
    last_n = max(1, last_n)
    recent = tasks[-last_n:]
    title = session.get("title") or "(untitled)"
    lines = [f"Resuming session: {title}", f"Prior tasks ({len(recent)} of {len(tasks)}):"]
    for i, t in enumerate(recent, start=1):
        status = t.get("status", "pending")
        task_text = t.get("task", "")
        summary = t.get("summary")
        line = f"  {i}. [{status}] {task_text}"
        if summary:
            line += f" \u2192 {summary}"
        lines.append(line)
    return "\n".join(lines)


def format_session_line(session: dict) -> str:
    """Render a one-line summary of a session for UI listings.

    Format: ``<id>  <updated_at iso>  <task_count> tasks  <title>``
    Kept deterministic so tests can assert the exact string shape.
    """
    import datetime as _dt
    sid = session.get("id", "?")[:12]
    updated = session.get("updated_at", 0)
    ts = _dt.datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M")
    n = len(session.get("tasks") or [])
    title = session.get("title") or "(untitled)"
    return f"{sid}  {ts}  {n:>3} tasks  {title}"


def delete(sid: str, sessions_dir: Path | None = None) -> bool:
    p = _path(sid, sessions_dir)
    if p.exists():
        p.unlink()
        return True
    return False
