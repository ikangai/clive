"""Tier 0 = category names + counts only. No tool descriptions."""
import pytest
from toolsets import build_tier0_summary, resolve_toolset, COMMANDS

def test_tier0_lists_active_categories_with_counts():
    """Given the active categories of a session, list them with tool counts."""
    resolved = resolve_toolset("standard")
    summary = build_tier0_summary(resolved["categories"])
    # Active categories appear
    for cat in resolved["categories"]:
        assert cat in summary, f"missing category {cat}"
    # Counts appear (e.g. "data(4)")
    assert "(" in summary and ")" in summary
    # Discovery hint is present
    assert "tool_info" in summary or "tools" in summary.lower()

def test_tier0_is_compact():
    """Tier 0 stays under 200 tokens even with all categories loaded."""
    from toolsets import CATEGORIES
    summary = build_tier0_summary(list(CATEGORIES.keys()))
    assert len(summary) // 4 < 200, \
        f"tier0 too large: {len(summary)//4} tokens"

def test_tier0_skips_unknown_categories():
    """Robust to typos — unknown categories are silently dropped."""
    summary = build_tier0_summary(["data", "not_a_real_category"])
    assert "not_a_real_category" not in summary
    assert "data" in summary
