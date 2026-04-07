"""Tests for context window trimming."""
from executor import _trim_messages


def test_short_conversation_unchanged():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "reply 1"},
    ]
    trimmed = _trim_messages(messages, max_user_turns=3)
    assert len(trimmed) == 3


def test_long_conversation_trimmed():
    """Bookend strategy: keeps first turn + last N-1 turns."""
    messages = [{"role": "system", "content": "system prompt"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"turn {i}"})
        messages.append({"role": "assistant", "content": f"reply {i}"})
    trimmed = _trim_messages(messages, max_user_turns=3)
    # system(1) + first_pair(2) + last 2 pairs(4) = 7
    assert len(trimmed) == 7
    assert trimmed[0]["role"] == "system"
    assert "turn 0" in trimmed[1]["content"]  # first turn preserved (bookend)
    assert "turn 9" in trimmed[-2]["content"]  # most recent


def test_preserves_first_and_last_turns():
    """Bookend: first turn (initial context) + last turns."""
    messages = [
        {"role": "system", "content": "important system prompt"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
    ]
    trimmed = _trim_messages(messages, max_user_turns=2)
    assert trimmed[0]["content"] == "important system prompt"
    assert "u1" in trimmed[1]["content"]  # first turn kept
    assert "u4" in trimmed[-2]["content"]  # most recent kept


def test_empty_messages():
    assert _trim_messages([], max_user_turns=3) == []


def test_system_only():
    messages = [{"role": "system", "content": "sys"}]
    assert _trim_messages(messages, max_user_turns=3) == messages
