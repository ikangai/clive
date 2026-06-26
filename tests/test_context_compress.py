"""Tests for progressive context compression."""
from context_compress import (
    compress_context,
    _format_turns_for_summary,
    extract_dead_ends,
)


def _make_conversation(n_turns, with_system=True):
    """Build a conversation with n user/assistant pairs."""
    messages = []
    if with_system:
        messages.append({"role": "system", "content": "system prompt"})
    for i in range(n_turns):
        messages.append({"role": "user", "content": f"screen {i}"})
        messages.append({"role": "assistant", "content": f"command {i}"})
    return messages


def test_short_conversation_unchanged():
    """Conversations within the limit are returned as-is."""
    msgs = _make_conversation(3)
    result = compress_context(msgs, max_user_turns=4)
    assert result == msgs


def test_short_conversation_exact_limit():
    """Exactly at the limit — no compression needed."""
    msgs = _make_conversation(4)
    result = compress_context(msgs, max_user_turns=4)
    assert result == msgs


def test_long_conversation_compressed_with_fake_fn():
    """Old turns are summarized, recent turns kept verbatim."""
    msgs = _make_conversation(8)
    calls = []

    def fake_compress(text):
        calls.append(text)
        return "summary of old turns"

    result = compress_context(msgs, max_user_turns=4, compress_fn=fake_compress)

    # Should have been called once
    assert len(calls) == 1

    # Structure: system + summary + last 3 user/assistant pairs
    assert result[0]["role"] == "system"
    assert "[Earlier conversation summary]" in result[1]["content"]
    assert "summary of old turns" in result[1]["content"]

    # Recent turns preserved verbatim (last 3 pairs = 6 messages)
    recent = result[2:]
    assert len(recent) == 6
    assert "screen 5" in recent[0]["content"]
    assert "screen 7" in recent[-2]["content"]


def test_preserves_last_n_user_turns():
    """The most recent N-1 user turns are kept intact."""
    msgs = _make_conversation(10)

    def fake_compress(text):
        return "compressed"

    result = compress_context(msgs, max_user_turns=3, compress_fn=fake_compress)

    # Last 2 user turns should be verbatim
    user_msgs = [m for m in result if m["role"] == "user" and "[Earlier" not in m["content"]]
    assert len(user_msgs) == 2
    assert "screen 8" in user_msgs[0]["content"]
    assert "screen 9" in user_msgs[1]["content"]


def test_no_compress_fn_falls_back_to_trim():
    """Without compress_fn, falls back to _trim_messages bookend strategy."""
    msgs = _make_conversation(10)
    result = compress_context(msgs, max_user_turns=3, compress_fn=None)

    # Should match _trim_messages behavior: system + first pair + last 2 pairs
    assert result[0]["role"] == "system"
    assert "screen 0" in result[1]["content"]  # first turn (bookend)
    assert "screen 9" in result[-2]["content"]  # most recent


def test_compress_fn_failure_falls_back():
    """If compress_fn raises, falls back to trim."""
    msgs = _make_conversation(8)

    def broken_compress(text):
        raise RuntimeError("LLM down")

    result = compress_context(msgs, max_user_turns=3, compress_fn=broken_compress)

    # Should still return a valid trimmed result
    assert result[0]["role"] == "system"
    assert len(result) > 0


def test_system_messages_preserved():
    """System messages always survive compression."""
    msgs = _make_conversation(8)
    msgs.insert(0, {"role": "system", "content": "extra system"})

    def fake_compress(text):
        return "compressed"

    result = compress_context(msgs, max_user_turns=3, compress_fn=fake_compress)

    system_msgs = [m for m in result if m["role"] == "system"]
    assert len(system_msgs) == 2
    assert any("extra system" in m["content"] for m in system_msgs)
    assert any("system prompt" in m["content"] for m in system_msgs)


def test_empty_messages():
    assert compress_context([], max_user_turns=3) == []


def test_format_turns_for_summary():
    turns = [
        {"role": "user", "content": "x" * 300},
        {"role": "assistant", "content": "echo hello"},
        {"role": "user", "content": "short screen"},
    ]
    result = _format_turns_for_summary(turns)

    lines = result.split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("[Screen]:")
    assert "..." in lines[0]  # truncated
    assert lines[1] == "[Command]: echo hello"
    assert lines[2] == "[Screen]: short screen"


def test_format_turns_for_summary_empty():
    assert _format_turns_for_summary([]) == ""


# --- Dead-end ledger (gh: survive command failures across compression) ---


def test_extract_dead_ends_finds_failed_command():
    """A failing screen pins the immediately-preceding command as a dead end."""
    turns = [
        {"role": "user", "content": "screen 0"},
        {"role": "assistant", "content": "frobnicate --all"},
        {"role": "user", "content": "bash: frobnicate: command not found"},
    ]
    assert extract_dead_ends(turns) == ["frobnicate --all"]


def test_extract_dead_ends_recognizes_signals():
    """Each documented failure signal pins its preceding command."""
    signals = [
        "bash: foo: command not found",
        "cat: nope.txt: No such file or directory",
        "Command 'x' returned non-zero exit status 1.",
        'Traceback (most recent call last):\n  File "a.py"',
        "error: pathspec 'main' did not match",
        "fatal: not a git repository",
    ]
    for i, screen in enumerate(signals):
        turns = [
            {"role": "assistant", "content": f"cmd-{i}"},
            {"role": "user", "content": screen},
        ]
        assert extract_dead_ends(turns) == [f"cmd-{i}"], screen


def test_extract_dead_ends_ignores_success():
    """A clean screen does not pin its command."""
    turns = [
        {"role": "assistant", "content": "ls"},
        {"role": "user", "content": "file1  file2  file3"},
    ]
    assert extract_dead_ends(turns) == []


def test_extract_dead_ends_dedupes_and_preserves_order():
    """Repeated dead ends collapse; first-seen order is kept."""
    turns = []
    for cmd in ["a", "b", "a", "c"]:
        turns.append({"role": "assistant", "content": cmd})
        turns.append({"role": "user", "content": "command not found"})
    assert extract_dead_ends(turns) == ["a", "b", "c"]


def test_extract_dead_ends_reparses_prior_summary_block():
    """Dead ends already pinned in a prior summary are recovered."""
    summary = {
        "role": "user",
        "content": (
            "[Earlier conversation summary]\n"
            "DEAD ENDS - already tried and FAILED, do not retry:\n"
            "- old-cmd-1\n"
            "- old-cmd-2\n"
            "\n"
            "Agent tried to set up the env and failed."
        ),
    }
    assert extract_dead_ends([summary]) == ["old-cmd-1", "old-cmd-2"]


def _conversation_with_failure(n_turns, fail_at):
    """Build n user/assistant pairs; the screen after command `fail_at` fails."""
    messages = [{"role": "system", "content": "system prompt"}]
    for i in range(n_turns):
        screen = f"screen {i}"
        # the screen at index i is the observation of command (i-1)
        if i - 1 == fail_at:
            screen = "bash: command not found"
        messages.append({"role": "user", "content": screen})
        messages.append({"role": "assistant", "content": f"command {i}"})
    return messages


def test_compress_emits_dead_ends_block():
    """Compression pins a failed command into the summary as a DEAD ENDS block."""
    msgs = _conversation_with_failure(8, fail_at=1)

    def fake_compress(text):
        return "summary of old turns"

    result = compress_context(msgs, max_user_turns=4, compress_fn=fake_compress)
    summary_content = result[1]["content"]

    assert "DEAD ENDS" in summary_content
    assert "command 1" in summary_content
    # The real summary still rides along.
    assert "summary of old turns" in summary_content


def test_dead_ends_accumulate_across_squashes():
    """A second compression recovers the dead end from the prior summary block."""
    msgs = _conversation_with_failure(8, fail_at=1)

    def fake_compress(text):
        return "summary"

    first = compress_context(msgs, max_user_turns=4, compress_fn=fake_compress)
    assert "command 1" in first[1]["content"]

    # Continue the conversation, then squash again. The earlier summary
    # (now at the front) must keep the dead end alive.
    continued = first + [
        {"role": "user", "content": f"more screen {i}"}
        for i in range(0)
    ]
    for i in range(4):
        continued.append({"role": "user", "content": f"later screen {i}"})
        continued.append({"role": "assistant", "content": f"later command {i}"})

    second = compress_context(continued, max_user_turns=4, compress_fn=fake_compress)
    second_summary = second[1]["content"]
    assert "DEAD ENDS" in second_summary
    assert "command 1" in second_summary


# --- No-progress circuit breaker (escalate a command that keeps failing) ---


def _repeat_failure_conversation(cmd, times, ok_pairs):
    """A failing `cmd` repeated `times`, then `ok_pairs` clean turns.

    Each failure is an assistant command followed by a failing screen; the
    trailing clean pairs push the failures back into the compressed window.
    """
    msgs = [{"role": "system", "content": "system prompt"}]
    for _ in range(times):
        msgs.append({"role": "assistant", "content": cmd})
        msgs.append({"role": "user", "content": f"Command '{cmd}' returned non-zero exit status 1."})
    for i in range(ok_pairs):
        msgs.append({"role": "assistant", "content": f"ok command {i}"})
        msgs.append({"role": "user", "content": f"ok screen {i}"})
    return msgs


def test_compress_emits_no_progress_marker_at_threshold():
    """A command that fails 3x is escalated to a distinct NO PROGRESS marker."""
    msgs = _repeat_failure_conversation("npm run build", times=3, ok_pairs=4)

    def fake_compress(text):
        return "summary of old turns"

    result = compress_context(msgs, max_user_turns=4, compress_fn=fake_compress)
    summary_content = result[1]["content"]

    # The stronger marker names the command and the failure count.
    assert "NO PROGRESS" in summary_content
    assert "npm run build" in summary_content
    assert "3x" in summary_content
    # Escalated commands move UP out of the plain dead-ends ledger.
    assert "DEAD ENDS" not in summary_content
    # The real summary still rides along.
    assert "summary of old turns" in summary_content


def test_no_progress_marker_sits_above_dead_ends():
    """The NO PROGRESS marker is pinned ABOVE the normal dead-ends list."""
    msgs = [{"role": "system", "content": "system prompt"}]
    # one command fails 3x (escalates), another fails once (stays a dead end)
    for _ in range(3):
        msgs.append({"role": "assistant", "content": "stuck-cmd"})
        msgs.append({"role": "user", "content": "stuck-cmd: command not found"})
    msgs.append({"role": "assistant", "content": "oneoff-cmd"})
    msgs.append({"role": "user", "content": "oneoff-cmd: command not found"})
    for i in range(4):
        msgs.append({"role": "assistant", "content": f"ok command {i}"})
        msgs.append({"role": "user", "content": f"ok screen {i}"})

    def fake_compress(text):
        return "summary"

    result = compress_context(msgs, max_user_turns=4, compress_fn=fake_compress)
    summary_content = result[1]["content"]

    assert "NO PROGRESS" in summary_content
    assert "stuck-cmd" in summary_content
    assert "DEAD ENDS" in summary_content
    assert "oneoff-cmd" in summary_content
    # marker first, then the ledger
    assert summary_content.index("NO PROGRESS") < summary_content.index("DEAD ENDS")
    # the escalated command is NOT duplicated as a plain dead-ends bullet
    assert "- stuck-cmd" not in summary_content


def test_single_failure_has_no_no_progress_marker():
    """A command that fails once gets only the DEAD ENDS line, no escalation."""
    msgs = _conversation_with_failure(8, fail_at=1)

    def fake_compress(text):
        return "summary of old turns"

    result = compress_context(msgs, max_user_turns=4, compress_fn=fake_compress)
    summary_content = result[1]["content"]

    assert "DEAD ENDS" in summary_content
    assert "command 1" in summary_content
    assert "NO PROGRESS" not in summary_content


def test_no_progress_marker_survives_next_squash():
    """The escalation count is re-mined from a prior summary across squashes."""
    msgs = _repeat_failure_conversation("npm run build", times=3, ok_pairs=4)

    def fake_compress(text):
        return "summary"

    first = compress_context(msgs, max_user_turns=4, compress_fn=fake_compress)
    assert "NO PROGRESS" in first[1]["content"]
    assert "3x" in first[1]["content"]

    # Continue the conversation and squash again. The earlier summary (now at
    # the front) must keep the NO PROGRESS marker AND its count alive.
    continued = list(first)
    for i in range(4):
        continued.append({"role": "assistant", "content": f"later command {i}"})
        continued.append({"role": "user", "content": f"later screen {i}"})

    second = compress_context(continued, max_user_turns=4, compress_fn=fake_compress)
    second_summary = second[1]["content"]
    assert "NO PROGRESS" in second_summary
    assert "npm run build" in second_summary
    assert "3x" in second_summary


def test_no_progress_count_escalates_across_squash():
    """A re-mined marker count keeps climbing when the command fails again."""
    msgs = _repeat_failure_conversation("npm run build", times=3, ok_pairs=4)

    def fake_compress(text):
        return "summary"

    first = compress_context(msgs, max_user_turns=4, compress_fn=fake_compress)
    assert "3x" in first[1]["content"]

    # The model retries the doomed command once more, then we squash again.
    continued = list(first)
    continued.append({"role": "assistant", "content": "npm run build"})
    continued.append({"role": "user", "content": "Command 'npm run build' returned non-zero exit status 1."})
    for i in range(4):
        continued.append({"role": "assistant", "content": f"later command {i}"})
        continued.append({"role": "user", "content": f"later screen {i}"})

    second = compress_context(continued, max_user_turns=4, compress_fn=fake_compress)
    second_summary = second[1]["content"]
    assert "NO PROGRESS" in second_summary
    assert "4x" in second_summary
    assert "3x" not in second_summary
