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


def delete(sid: str, sessions_dir: Path | None = None) -> bool:
    p = _path(sid, sessions_dir)
    if p.exists():
        p.unlink()
        return True
    return False
