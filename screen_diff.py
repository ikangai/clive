"""Screen diff utility for the read loop.

Instead of sending the full tmux screen every turn, compute what changed
and send only the delta. This cuts token usage by 60-80% after turn 1.

The diff is designed for LLM consumption:
- First capture: full screen (no prior state)
- No change: "[Screen unchanged]"
- Small change: only new/changed lines with context
- Large change (>50% different): full screen (diff would be confusing)
- Capped at MAX_DIFF_LINES to prevent context bloat
"""
import difflib

MAX_DIFF_LINES = 60


def compute_screen_diff(
    prev_screen: str | None,
    curr_screen: str,
    context_lines: int = 1,
) -> str:
    """Compute a compact diff between two screen captures.

    Returns a string suitable for sending to the LLM as screen context.
    Caps output at MAX_DIFF_LINES to prevent context bloat.
    """
    # First capture — send everything (capped)
    if prev_screen is None:
        if not curr_screen.strip():
            return "[Screen empty — waiting for output]"
        lines = curr_screen.splitlines()
        if len(lines) > MAX_DIFF_LINES:
            return "\n".join(lines[-MAX_DIFF_LINES:]) + f"\n[...truncated, showing last {MAX_DIFF_LINES} lines]"
        return curr_screen

    # No change
    if prev_screen == curr_screen:
        return "[Screen unchanged — command may have produced no visible output]"

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

    # Large change — send full screen (capped)
    if total_changed > len(curr_lines) * 0.5:
        lines = curr_screen.splitlines()
        if len(lines) > MAX_DIFF_LINES:
            return "\n".join(lines[-MAX_DIFF_LINES:]) + f"\n[...truncated, showing last {MAX_DIFF_LINES} lines]"
        return curr_screen

    # Annotate diff with progress signal
    if added > 20:
        hint = " — large output, consider head/tail to inspect"
    elif added == 0 and removed > 0:
        hint = " — content cleared"
    elif added <= 2 and removed == 0:
        hint = " — minimal change"
    else:
        hint = ""

    # Build compact diff, capped at MAX_DIFF_LINES
    parts = [f"[Screen update: +{added} -{removed} lines{hint}]"]
    line_count = 1
    for line in diff:
        if line.startswith("@@") or line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            parts.append(f"  {line[1:]}")
            line_count += 1
        elif line.startswith(" "):
            parts.append(f"  {line[1:]}")
            line_count += 1
        if line_count >= MAX_DIFF_LINES:
            parts.append(f"  [...diff truncated at {MAX_DIFF_LINES} lines]")
            break

    return "\n".join(parts)
