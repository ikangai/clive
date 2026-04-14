"""Tests for progressive context compression."""
from context_compress import compress_context, _format_turns_for_summary


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
