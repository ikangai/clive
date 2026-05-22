"""Tier 0 = category names + counts only. No tool descriptions.
Tier 1 = per-category tool name listing. No descriptions."""
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
    assert "tool_info" in summary

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

def test_tier0_empty_returns_empty_string():
    """Empty or all-unknown categories list returns empty string, not a vacuous header."""
    from toolsets import build_tier0_summary
    assert build_tier0_summary([]) == ""
    assert build_tier0_summary(["not_real"]) == ""

def test_tier1_lists_names_per_category():
    """Tier 1: per-category name listing, no descriptions."""
    from toolsets import build_tier1_names
    summary = build_tier1_names(["data"])
    # Category header
    assert "data:" in summary
    # All data commands listed
    for name in ("jq", "rg", "mlr", "sqlite3"):
        assert name in summary
    # NO descriptions leak in
    assert "JSON processor" not in summary

def test_tier1_handles_multiple_categories():
    from toolsets import build_tier1_names
    summary = build_tier1_names(["data", "web"])
    assert "data:" in summary
    assert "web:" in summary

def test_tier1_includes_panes_and_endpoints():
    """A category can offer panes and endpoints too — list all surfaces."""
    from toolsets import build_tier1_names
    summary = build_tier1_names(["info"])
    # info has endpoint-only category
    assert "weather" in summary or "hackernews" in summary
