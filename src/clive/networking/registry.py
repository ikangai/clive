"""File-based instance registry for clive.

Each running clive instance with a --name gets a JSON file in ~/.clive/instances/.
Provides discovery, liveness checking (via os.kill(pid, 0)), and stale entry pruning.
"""
import json
import os
import time
from pathlib import Path

DEFAULT_REGISTRY_DIR = Path.home() / ".clive" / "instances"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def register(name: str, pid: int, tmux_session: str, tmux_socket: str,
             toolset: str, task: str, conversational: bool, session_dir: str,
             registry_dir: Path | None = None) -> Path:
    d = registry_dir or DEFAULT_REGISTRY_DIR
    d.mkdir(parents=True, exist_ok=True)
    entry = {
        "name": name,
        "pid": pid,
        "tmux_session": tmux_session,
        "tmux_socket": tmux_socket,
        "toolset": toolset,
        "task": task,
        "conversational": conversational,
        "session_dir": session_dir,
        "started_at": time.time(),
    }
    p = d / f"{name}.json"
    p.write_text(json.dumps(entry, indent=2))
    return p


def deregister(name: str, registry_dir: Path | None = None) -> bool:
    d = registry_dir or DEFAULT_REGISTRY_DIR
    p = d / f"{name}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def list_instances(registry_dir: Path | None = None) -> list[dict]:
    d = registry_dir or DEFAULT_REGISTRY_DIR
    if not d.exists():
        return []
    result = []
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            f.unlink(missing_ok=True)
            continue
        if _pid_alive(data.get("pid", -1)):
            result.append(data)
        else:
            f.unlink(missing_ok=True)
    return result


def get_instance(name: str, registry_dir: Path | None = None) -> dict | None:
    d = registry_dir or DEFAULT_REGISTRY_DIR
    p = d / f"{name}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        p.unlink(missing_ok=True)
        return None
    if _pid_alive(data.get("pid", -1)):
        return data
    p.unlink(missing_ok=True)
    return None


def is_name_available(name: str, registry_dir: Path | None = None) -> bool:
    return get_instance(name, registry_dir=registry_dir) is None
