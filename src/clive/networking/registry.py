"""File-based instance registry for clive.

Each running clive instance with a --name gets a JSON file in ~/.clive/instances/.
Provides discovery, liveness checking (via os.kill(pid, 0)), and stale entry pruning.
"""
import json
import os
import time
from pathlib import Path

DEFAULT_REGISTRY_DIR = Path.home() / ".clive" / "instances"


def _secure_dir(d: Path) -> None:
    """Create (or tighten) a registry dir to 0700.

    Entries carry the instance's frame nonce and a tmux socket path that
    is later interpolated into a shell command, so they are private to the
    owning user. ``mkdir(mode=...)`` only applies on creation and is masked
    by umask, so chmod an existing directory too (gh#48).
    """
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if (d.stat().st_mode & 0o777) != 0o700:
            d.chmod(0o700)
    except OSError:
        pass  # best-effort: a non-owned dir shouldn't crash registration


def _write_private(p: Path, text: str) -> None:
    """Write a registry entry readable only by its owner (0600)."""
    # Open with the restrictive mode up front so the contents are never
    # briefly world-readable between create and chmod.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        fh = os.fdopen(fd, "w")  # takes ownership of fd from here on
    except BaseException:
        os.close(fd)
        raise
    with fh:
        fh.write(text)
    try:
        p.chmod(0o600)  # tighten a pre-existing looser file
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def register(name: str, pid: int, tmux_session: str, tmux_socket: str,
             toolset: str, task: str, conversational: bool, session_dir: str,
             registry_dir: Path | None = None,
             role: str | None = None,
             socket_path: str | None = None,
             frame_nonce: str | None = None) -> Path:
    d = registry_dir or DEFAULT_REGISTRY_DIR
    _secure_dir(d)
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
    # Optional fields (omitted when unset so consumers that don't know
    # about them never see a surprising key — see §9.6).
    if role is not None:
        entry["role"] = role
    if socket_path is not None:
        entry["socket_path"] = socket_path
    # The session nonce another clive needs to authenticate this instance's
    # frames over a local `clive@<name>` conversation. There is no SSH hop
    # to inject one, so the instance publishes its own here — which is why
    # the entry is written 0600 (gh#46, gh#48).
    if frame_nonce is not None:
        entry["frame_nonce"] = frame_nonce
    p = d / f"{name}.json"
    _write_private(p, json.dumps(entry, indent=2))
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
