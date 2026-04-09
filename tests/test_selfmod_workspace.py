"""Tests for selfmod/workspace.py apply_atomic() function."""

import subprocess
from pathlib import Path

import pytest

from selfmod.workspace import apply_atomic


def _run(cwd, *args):
    """Run a command in a directory."""
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path):
    """Create a minimal git repo with an initial commit and a passing test."""
    _run(tmp_path, "git", "init")
    _run(tmp_path, "git", "config", "user.email", "test@test.com")
    _run(tmp_path, "git", "config", "user.name", "Test")

    # Create a minimal passing test
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_ok.py").write_text(
        "def test_pass():\n    assert True\n"
    )

    _run(tmp_path, "git", "add", "-A")
    _run(tmp_path, "git", "commit", "-m", "initial")


# ── Tests ────────────────────────────────────────────────────────────────


def test_apply_atomic_creates_selfmod_branch(tmp_path):
    """apply_atomic should create a branch with the selfmod/ prefix."""
    _init_repo(tmp_path)

    result = apply_atomic(
        files={"hello.txt": "world\n"},
        proposal_id="test123",
        project_root=tmp_path,
    )

    assert result["branch"] == "selfmod/test123"


def test_apply_atomic_merges_on_success(tmp_path):
    """When tests pass, changes should be merged back to the original branch."""
    _init_repo(tmp_path)

    result = apply_atomic(
        files={"newfile.txt": "content\n"},
        proposal_id="good_change",
        project_root=tmp_path,
    )

    assert result["success"] is True
    assert "merged" in result["message"].lower() or "success" in result["message"].lower()

    # The file should exist on the current branch after merge
    assert (tmp_path / "newfile.txt").read_text() == "content\n"

    # The selfmod branch should be deleted after successful merge
    branches = subprocess.run(
        ["git", "branch"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "selfmod/good_change" not in branches


def test_apply_atomic_rolls_back_on_failure(tmp_path):
    """When tests fail, the branch should be deleted and changes reverted."""
    _init_repo(tmp_path)

    # Add a test file that will always fail
    files = {
        "tests/test_fail.py": "def test_always_fails():\n    assert False, 'deliberate failure'\n",
        "should_not_exist.txt": "this file should be reverted\n",
    }

    result = apply_atomic(
        files=files,
        proposal_id="bad_change",
        project_root=tmp_path,
    )

    assert result["success"] is False
    assert "fail" in result["message"].lower()

    # The failing test file should NOT exist on the original branch
    assert not (tmp_path / "tests" / "test_fail.py").exists()
    assert not (tmp_path / "should_not_exist.txt").exists()

    # The selfmod branch should be deleted
    branches = subprocess.run(
        ["git", "branch"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "selfmod/bad_change" not in branches


def test_apply_atomic_preserves_original_on_failure(tmp_path):
    """Original branch content should be untouched after a failed apply."""
    _init_repo(tmp_path)

    # Create a file on the original branch
    (tmp_path / "original.txt").write_text("keep me\n")
    _run(tmp_path, "git", "add", "-A")
    _run(tmp_path, "git", "commit", "-m", "add original file")

    # Attempt a change that will fail tests
    files = {
        "tests/test_boom.py": "def test_boom():\n    raise RuntimeError('boom')\n",
    }

    result = apply_atomic(
        files=files,
        proposal_id="boom",
        project_root=tmp_path,
    )

    assert result["success"] is False
    # Original file still intact
    assert (tmp_path / "original.txt").read_text() == "keep me\n"
