"""Git-based workspace management for safe self-modification."""

import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def snapshot(label: str = "") -> str:
    """Create a git tag snapshot of current state. Returns tag name."""
    tag = f"selfmod-snap-{int(time.time())}"
    if label:
        tag += f"-{label}"

    _git("add", "-A")
    # Only commit if there are changes
    status = _git("status", "--porcelain")
    if status.strip():
        _git("commit", "-m", f"selfmod snapshot: {label or 'pre-modification'}", "--allow-empty")

    _git("tag", tag)
    return tag


def rollback(tag: str | None = None) -> str:
    """Roll back to a snapshot. Returns the tag rolled back to."""
    if tag is None:
        # Find most recent selfmod snapshot
        tags = _git("tag", "-l", "selfmod-snap-*", "--sort=-creatordate")
        tag_list = [t.strip() for t in tags.strip().split("\n") if t.strip()]
        if not tag_list:
            raise RuntimeError("No selfmod snapshots found")
        tag = tag_list[0]

    _git("checkout", tag, "--", ".")
    _git("checkout", tag, "--", "selfmod/")
    return tag


def list_snapshots() -> list[dict]:
    """List all selfmod snapshots."""
    tags = _git("tag", "-l", "selfmod-snap-*", "--sort=-creatordate")
    result = []
    for tag in tags.strip().split("\n"):
        tag = tag.strip()
        if not tag:
            continue
        # Get tag date
        date = _git("log", "-1", "--format=%ci", tag).strip()
        result.append({"tag": tag, "date": date})
    return result


def apply_changes(files: dict[str, str]) -> None:
    """Write proposed changes to disk."""
    for filepath, content in files.items():
        path = PROJECT_ROOT / filepath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _git(*args: str) -> str:
    """Run a git command in the project root."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 and "nothing to commit" not in result.stdout:
        import logging
        log = logging.getLogger(__name__)
        if result.stderr.strip():
            log.warning("git %s failed (exit %d): %s", args[0], result.returncode, result.stderr.strip())
    return result.stdout
