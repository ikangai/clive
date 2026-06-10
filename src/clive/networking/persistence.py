"""Named-instance session persistence (gh#13).

Always-on `--name foo` instances lose their state on crash or reboot: the
live registry (``networking/registry.py``) prunes an entry the instant its
pid is dead, so nothing is left to restore from. This module keeps a
*restorable session snapshot* that survives process death — written
alongside registration, never liveness-pruned — plus a helper that
cross-references the registry to report which instances can be restored.

This is clive-native persistence: the snapshot records the toolset, pane
layout, and session dir, which is exactly what ``setup_session`` needs to
rebuild the tmux session on the next launch. No external tmux-resurrect /
TPM dependency is required.

Snapshots live in ``~/.clive/persist/<name>.json``, separate from the
liveness-pruned ``~/.clive/instances/`` registry so the two concerns don't
interfere.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PERSIST_DIR = Path.home() / ".clive" / "persist"

# Snapshot format version — bump when the on-disk shape changes so an old
# snapshot can be detected and migrated/ignored rather than misread.
SCHEMA_VERSION = 1

# Instance names become filenames; keep them to a safe charset so a name
# can never escape the persist dir (defence in depth — mirrors the spirit
# of discovery._check_tool_name).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"unsafe instance name {name!r}: must match {_NAME_RE.pattern}"
        )


def save_snapshot(name: str, toolset: str, session_dir: str,
                  tmux_session: str, tmux_socket: str, *,
                  task: str = "", conversational: bool = True,
                  panes: list | None = None,
                  persist_dir: Path | None = None) -> Path:
    """Write a restorable snapshot for ``name``. Overwrites any prior one.

    Atomic via write-tmp + replace so a crash mid-write never leaves a
    half-written snapshot that would later be pruned as corrupt.
    """
    _check_name(name)
    d = persist_dir or DEFAULT_PERSIST_DIR
    d.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema": SCHEMA_VERSION,
        "name": name,
        "toolset": toolset,
        "session_dir": session_dir,
        "tmux_session": tmux_session,
        "tmux_socket": tmux_socket,
        "task": task,
        "conversational": conversational,
        "panes": panes or [],
        "saved_at": time.time(),
    }
    p = d / f"{name}.json"
    tmp = d / f"{name}.json.part"
    tmp.write_text(json.dumps(entry, indent=2))
    tmp.replace(p)
    return p


def load_snapshot(name: str, persist_dir: Path | None = None) -> dict | None:
    """Load the snapshot for ``name``, or ``None`` if missing/corrupt.

    A corrupt snapshot is pruned so it doesn't linger as a phantom
    restorable instance.
    """
    _check_name(name)
    d = persist_dir or DEFAULT_PERSIST_DIR
    p = d / f"{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.debug("pruning corrupt snapshot %s", p, exc_info=True)
        p.unlink(missing_ok=True)
        return None


def list_snapshots(persist_dir: Path | None = None) -> list[dict]:
    """All persisted snapshots (corrupt ones pruned and skipped)."""
    d = persist_dir or DEFAULT_PERSIST_DIR
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            f.unlink(missing_ok=True)
    return out


def clear_snapshot(name: str, persist_dir: Path | None = None) -> bool:
    """Delete the snapshot for ``name``. Returns True if one was removed."""
    _check_name(name)
    d = persist_dir or DEFAULT_PERSIST_DIR
    p = d / f"{name}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def restorable_instances(persist_dir: Path | None = None,
                         registry_dir: Path | None = None) -> list[dict]:
    """Snapshots whose named instance is NOT currently live.

    Cross-references ``registry.get_instance`` (which returns ``None`` for a
    dead/absent instance) so the result is exactly the set of instances a
    user could relaunch from a saved spec.
    """
    import registry
    out = []
    for snap in list_snapshots(persist_dir=persist_dir):
        name = snap.get("name")
        if not name:
            continue
        if registry.get_instance(name, registry_dir=registry_dir) is None:
            out.append(snap)
    return out
