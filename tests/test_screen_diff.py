"""Tests for screen diff utility."""
from screen_diff import compute_screen_diff


def test_first_capture_returns_full_screen():
    diff = compute_screen_diff(None, "line1\nline2\nline3")
    assert "line1" in diff
    assert "line2" in diff
    assert "line3" in diff


def test_identical_screens_returns_no_change():
    screen = "$ ls\nfile1.txt\nfile2.txt\n[AGENT_READY] $"
    diff = compute_screen_diff(screen, screen)
    assert "no change" in diff.lower() or "unchanged" in diff.lower()


def test_new_lines_shown():
    prev = "$ ls\n[AGENT_READY] $"
    curr = "$ ls\nfile1.txt\nfile2.txt\n[AGENT_READY] $"
    diff = compute_screen_diff(prev, curr)
    assert "file1.txt" in diff
    assert "file2.txt" in diff


def test_removed_lines_not_included():
    prev = "line1\nline2\nline3"
    curr = "line1\nline3"
    diff = compute_screen_diff(prev, curr)
    assert "line1" in diff
    assert "line3" in diff


def test_diff_is_shorter_than_full_screen():
    prev = "\n".join(f"line {i}" for i in range(50))
    curr = prev + "\nnew line 50\nnew line 51"
    diff = compute_screen_diff(prev, curr)
    assert len(diff) < len(curr)


def test_large_change_returns_full_screen():
    prev = "old content"
    curr = "\n".join(f"new line {i}" for i in range(30))
    diff = compute_screen_diff(prev, curr)
    assert "new line 0" in diff
    assert "new line 29" in diff
