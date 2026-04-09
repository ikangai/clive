"""Git-based workspace management for safe self-modification."""

import subprocess
import sys
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


def _git_in(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in the specified directory. Returns CompletedProcess."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )


def apply_atomic(
    files: dict[str, str],
    proposal_id: str,
    project_root: Path | None = None,
) -> dict:
    """Apply changes atomically using a git branch.

    Returns {"success": bool, "message": str, "branch": str}.
    """
    root = project_root or PROJECT_ROOT
    branch = f"selfmod/{proposal_id}"

    # Determine current branch to return to after the operation
    r = _git_in(root, "rev-parse", "--abbrev-ref", "HEAD")
    if r.returncode != 0:
        return {"success": False, "message": f"Not a git repo: {r.stderr.strip()}", "branch": branch}
    original_branch = r.stdout.strip()

    # 1. Create and switch to the selfmod branch
    r = _git_in(root, "checkout", "-b", branch)
    if r.returncode != 0:
        return {"success": False, "message": f"Branch creation failed: {r.stderr.strip()}", "branch": branch}

    try:
        # 2. Apply file changes
        for filepath, content in files.items():
            path = root / filepath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        # Stage and commit
        _git_in(root, "add", "-A")
        r = _git_in(root, "commit", "-m", f"selfmod: apply proposal {proposal_id}")
        if r.returncode != 0 and "nothing to commit" not in r.stdout:
            raise RuntimeError(f"Commit failed: {r.stderr.strip()}")

        # 3. Run tests
        test_result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-x", "--tb=short", "-q"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if test_result.returncode == 0:
            # 4a. Tests passed — merge back
            _git_in(root, "checkout", original_branch)
            r = _git_in(root, "merge", branch, "--no-edit")
            if r.returncode != 0:
                # Merge conflict — abort and clean up
                _git_in(root, "merge", "--abort")
                _git_in(root, "branch", "-D", branch)
                return {
                    "success": False,
                    "message": f"Merge conflict: {r.stderr.strip()}",
                    "branch": branch,
                }
            # Clean up the branch
            _git_in(root, "branch", "-d", branch)
            return {
                "success": True,
                "message": f"Successfully merged proposal {proposal_id}",
                "branch": branch,
            }
        else:
            # 4b. Tests failed — rollback
            test_output = (test_result.stdout + test_result.stderr).strip()
            _git_in(root, "checkout", original_branch)
            _git_in(root, "branch", "-D", branch)
            return {
                "success": False,
                "message": f"Tests failed:\n{test_output}",
                "branch": branch,
            }

    except Exception as exc:
        # Ensure we always return to the original branch and clean up
        _git_in(root, "checkout", original_branch)
        _git_in(root, "branch", "-D", branch)
        return {
            "success": False,
            "message": f"Error during apply: {exc}",
            "branch": branch,
        }


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
