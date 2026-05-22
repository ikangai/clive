"""Classify an unknown tool into one of the existing categories."""
import pytest
from toolsets import classify_tool_to_category

@pytest.mark.parametrize("name,desc,expected", [
    ("xq", "Command-line XML processor like jq for XML", "data"),
    ("httpie", "Modern HTTP client with intuitive syntax", "web"),
    ("imageoptim", "Optimize PNG and JPEG image files in place", "images"),
    ("zoxide", "Smarter cd command that learns your habits", "core"),
    ("lazygit", "Terminal UI for git commands", "dev"),
])
def test_classify_known_shape(name, desc, expected):
    assert classify_tool_to_category(name, desc) == expected

def test_classify_returns_none_for_unclassifiable():
    """No category should match a completely random description."""
    result = classify_tool_to_category("frobnicator",
                                       "asdf qwer zxcv abcd efgh")
    assert result is None


def test_classify_ties_resolve_deterministically():
    """When two categories score equal, dict insertion order wins (data before web)."""
    # 'json' (data) + 'http' (web), both score 1 — data must win.
    assert classify_tool_to_category("xy", "json over http") == "data"
