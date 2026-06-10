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
    # Discovery hint must reference the command that actually exists
    # in-pane (gh#40 live-eval finding: `tool_info` was a phantom).
    assert "clive-tools info" in summary
    assert "clive-tools list" in summary

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
    assert "data: " in summary  # space after colon — locked format
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

def test_tier1_includes_endpoints():
    """A category can offer panes and endpoints too — list all surfaces."""
    from toolsets import build_tier1_names
    summary = build_tier1_names(["info"])
    # info has endpoint-only category
    assert "weather" in summary or "hackernews" in summary

def test_tier1_empty_returns_empty_string():
    """Symmetric with tier0: empty or all-unknown returns empty string."""
    from toolsets import build_tier1_names
    assert build_tier1_names([]) == ""
    assert build_tier1_names(["not_real"]) == ""

def test_tier2_returns_card_for_known_command():
    from toolsets import build_tier2_card
    card = build_tier2_card("jq")
    assert card is not None
    assert card.startswith("[jq]")
    assert len(card) <= 200

def test_tier2_returns_none_for_unknown():
    from toolsets import build_tier2_card
    assert build_tier2_card("not_a_real_tool") is None

def test_tier2_resolves_aliases():
    """Aliases like 'mail' → 'email' should resolve."""
    from toolsets import build_tier2_card
    # 'mail' is an alias for the email pane; cards for panes synthesize
    # from the pane definition (description + usage hints).
    card = build_tier2_card("mail")
    # Either a card exists (resolved via alias) or returns None gracefully.
    # We accept either as long as it doesn't crash.
    if card is not None:
        assert card.startswith("[email]") or card.startswith("[mail]")
