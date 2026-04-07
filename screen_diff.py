"""Screen diff utility for the read loop.

Instead of sending the full tmux screen every turn, compute what changed
and send only the delta. This cuts token usage by 60-80% after turn 1.
"""
import difflib


def compute_screen_diff(
    prev_screen: str | None,
    curr_screen: str,
    context_lines: int = 1,
) -> str:
    """Compute a compact diff between two screen captures.

    Returns a string suitable for sending to the LLM as screen context.
    """
    if prev_screen is None:
        return curr_screen

    if prev_screen == curr_screen:
        return "[Screen unchanged]"

    prev_lines = prev_screen.splitlines()
    curr_lines = curr_screen.splitlines()

    diff = list(difflib.unified_diff(
        prev_lines, curr_lines,
        n=context_lines,
        lineterm="",
    ))

    if not diff:
        return "[Screen unchanged]"

    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    total_changed = added + removed

    if total_changed > len(curr_lines) * 0.5:
        return curr_screen

    parts = [f"[Screen update: +{added} -{removed} lines]"]
    for line in diff:
        if line.startswith("@@") or line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            parts.append(f"  {line[1:]}")
        elif line.startswith(" "):
            parts.append(f"  {line[1:]}")

    return "\n".join(parts)
