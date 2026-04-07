"""Tests for interactive loop features: no-change stop, wait command, batched exit."""
from models import Subtask, SubtaskStatus
from executor import parse_command


# ─── Wait command parsing ─────────────────────────────────────────────────────

def test_parse_wait_command():
    cmd = parse_command('<cmd type="wait">5</cmd>')
    assert cmd["type"] == "wait"
    assert cmd["value"] == "5"


def test_parse_wait_no_pane_needed():
    cmd = parse_command('<cmd type="wait">2</cmd>')
    assert cmd["pane"] is None


# ─── No-change stop conditions ────────────────────────────────────────────────

def test_no_change_count_resets_on_read_file():
    """read_file operations should reset the no-change counter."""
    # This tests the logic, not the actual executor loop
    no_change_count = 2
    cmd_type = "read_file"
    if cmd_type in ("read_file", "write_file"):
        no_change_count = 0
    assert no_change_count == 0


def test_no_change_count_resets_on_write_file():
    cmd_type = "write_file"
    no_change_count = 2
    if cmd_type in ("read_file", "write_file"):
        no_change_count = 0
    assert no_change_count == 0


def test_no_change_count_not_reset_on_none():
    cmd_type = "none"
    no_change_count = 2
    if cmd_type in ("read_file", "write_file"):
        no_change_count = 0
    assert no_change_count == 2


# ─── Batched exit code parsing ────────────────────────────────────────────────

def test_parse_exit_code_from_combined_marker():
    """The batched script execution puts EXIT:N and marker on same line."""
    marker = "___DONE_task1_abc1___"
    screen = f"some output\nmore output\nEXIT:0 {marker}\n[AGENT_READY] $"

    exit_code = None
    for line in screen.splitlines():
        if marker in line and "EXIT:" in line:
            try:
                exit_part = line.split("EXIT:")[1].split()[0]
                exit_code = int(exit_part)
            except (ValueError, IndexError):
                pass
    assert exit_code == 0


def test_parse_exit_code_failure():
    marker = "___DONE_task1_abc1___"
    screen = f"error: file not found\nEXIT:1 {marker}\n[AGENT_READY] $"

    exit_code = None
    for line in screen.splitlines():
        if marker in line and "EXIT:" in line:
            try:
                exit_part = line.split("EXIT:")[1].split()[0]
                exit_code = int(exit_part)
            except (ValueError, IndexError):
                pass
    assert exit_code == 1


# ─── Screen diff integration ──────────────────────────────────────────────────

def test_screen_diff_with_scrollback():
    """Verify diff works correctly with longer screen captures."""
    from screen_diff import compute_screen_diff

    prev = "\n".join([f"scrollback line {i}" for i in range(50)] + ["[AGENT_READY] $"])
    curr = prev + "\n$ ls\nfile1.txt\nfile2.txt\n[AGENT_READY] $"

    diff = compute_screen_diff(prev, curr)
    assert "file1.txt" in diff
    assert len(diff) < len(curr)  # diff should be shorter


# ─── Context trimming with diffs ──────────────────────────────────────────────

def test_trim_preserves_recent_diffs():
    from executor import _trim_messages

    messages = [{"role": "system", "content": "system prompt"}]
    for i in range(8):
        messages.append({"role": "user", "content": f"[Screen update: +2 lines] turn {i}"})
        messages.append({"role": "assistant", "content": f"<cmd type='shell'>cmd {i}</cmd>"})

    trimmed = _trim_messages(messages, max_user_turns=4)
    # Should keep system + last 4 user-assistant pairs
    assert trimmed[0]["role"] == "system"
    assert "turn 7" in trimmed[-2]["content"]  # most recent
    assert "turn 4" in trimmed[1]["content"]   # oldest kept
    assert "turn 3" not in str(trimmed)        # dropped
