"""Progressive context compression for interactive runner conversations.

Instead of dropping old turns entirely (_trim_messages bookend strategy),
this module summarizes them via a cheap LLM call, preserving information
while keeping the context window small.
"""

import logging

log = logging.getLogger(__name__)


def _format_turns_for_summary(turns: list[dict]) -> str:
    """Format user/assistant message pairs into compact text for summarization.

    User turns (screen observations) are labeled [Screen] and truncated.
    Assistant turns (commands) are labeled [Command].
    """
    lines = []
    for msg in turns:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            # Truncate screen observations — they're verbose
            snippet = content[:200]
            if len(content) > 200:
                snippet += "..."
            lines.append(f"[Screen]: {snippet}")
        elif role == "assistant":
            lines.append(f"[Command]: {content}")
    return "\n".join(lines)


def compress_context(
    messages: list[dict],
    max_user_turns: int = 4,
    compress_fn=None,
) -> list[dict]:
    """Compress old conversation turns, keeping recent ones verbatim.

    If the conversation is short enough, returns messages unchanged.
    If compress_fn is provided, old turns are summarized via that function.
    If compress_fn is None, falls back to the bookend _trim_messages strategy.

    Args:
        messages: Full conversation history (system + user/assistant pairs).
        max_user_turns: Maximum user turns to keep before compressing.
        compress_fn: callable(str) -> str that summarizes text, or None.

    Returns:
        Compressed message list: system + [summary] + recent turns.
    """
    if not messages:
        return messages

    system = [m for m in messages if m["role"] == "system"]
    conversation = [m for m in messages if m["role"] != "system"]

    user_indices = [i for i, m in enumerate(conversation) if m["role"] == "user"]

    if len(user_indices) <= max_user_turns:
        return messages

    # Fall back to trim if no compressor available
    if compress_fn is None:
        from interactive_runner import _trim_messages
        return _trim_messages(messages, max_user_turns=max_user_turns)

    # Split: old turns to compress, recent turns to keep verbatim
    keep_count = max_user_turns - 1 if max_user_turns > 1 else 1
    cutoff_idx = user_indices[-keep_count]
    old_turns = conversation[:cutoff_idx]
    recent_turns = conversation[cutoff_idx:]

    # Summarize old turns
    old_text = _format_turns_for_summary(old_turns)
    try:
        summary = compress_fn(old_text)
    except Exception:
        log.warning("Context compression failed, falling back to trim")
        from interactive_runner import _trim_messages
        return _trim_messages(messages, max_user_turns=max_user_turns)

    summary_msg = {
        "role": "user",
        "content": f"[Earlier conversation summary]\n{summary}",
    }

    return system + [summary_msg] + recent_turns


def make_llm_compressor(client, model: str | None = None):
    """Create a compress_fn that uses a cheap LLM to summarize old turns.

    Args:
        client: OpenAI-compatible client instance.
        model: Model to use. Defaults to CLASSIFIER_MODEL.

    Returns:
        callable(str) -> str
    """
    if model is None:
        from llm import CLASSIFIER_MODEL
        model = CLASSIFIER_MODEL

    def _compress(text: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this terminal session history in 2-3 concise sentences. "
                        "Focus on: what was attempted, what succeeded, what failed, "
                        "and the current state. Omit raw screen content."
                    ),
                },
                {"role": "user", "content": text},
            ],
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()

    return _compress
