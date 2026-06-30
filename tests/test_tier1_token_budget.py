"""Tier-1 per-category token-budget cap (gh#39).

`build_tier1_names` documents a ~50-token/category contract, but joined ALL
tool names onto one line with no size guard. As the 18 open "add tools"
issues (#21-#38) pack categories like core/data, that silently blew the
token guarantee the four-tier registry exists to provide. Each category line
is now capped at a token budget; overflow emits the first names plus a
"+N more (run: clive-tools list <cat>)" pointer so omitted tools stay
reachable via the existing expansion command.
"""
import re
import pytest
from toolsets import build_tier1_names, TIER1_CATEGORY_TOKEN_BUDGET


def _approx_tokens(text: str) -> int:
    # Same 4-chars/token heuristic the registry token tests use.
    return len(text) // 4


def test_oversized_category_is_capped_at_token_budget(monkeypatch):
    """A category packed past the budget truncates to fit ~50 tokens."""
    import toolsets
    fat = [f"tool{i:02d}" for i in range(40)]
    monkeypatch.setitem(toolsets.CATEGORIES, "fat",
                        {"panes": [], "commands": fat, "endpoints": []})
    summary = build_tier1_names(["fat"])
    line = next(l for l in summary.splitlines() if l.startswith("fat:"))
    assert _approx_tokens(line) <= TIER1_CATEGORY_TOKEN_BUDGET, \
        f"capped line still {_approx_tokens(line)} tokens"


def test_oversized_category_emits_more_pointer(monkeypatch):
    """Overflow points back at the expansion command, naming the category."""
    import toolsets
    fat = [f"tool{i:02d}" for i in range(40)]
    monkeypatch.setitem(toolsets.CATEGORIES, "fat",
                        {"panes": [], "commands": fat, "endpoints": []})
    line = next(l for l in build_tier1_names(["fat"]).splitlines()
                if l.startswith("fat:"))
    # The first tool stays listed; a clearly-omitted late tool does not.
    assert "tool00" in line
    assert "tool39" not in line
    # Pointer references the real expansion command for this category.
    assert "run: clive-tools list fat" in line
    # The +N count equals the tools actually dropped.
    shown = line.split("+", 1)[0]
    n_shown = shown.count("tool")
    m = re.search(r"\+(\d+) more", line)
    assert m is not None
    assert int(m.group(1)) == 40 - n_shown


def test_small_category_is_untouched():
    """Categories within budget keep the full line and grow no pointer."""
    summary = build_tier1_names(["data"])
    assert "more (run:" not in summary
    # All data tools still listed in full.
    for name in ("jq", "rg", "mlr", "sqlite3"):
        assert name in summary


def test_at_least_one_name_when_single_tool_overflows(monkeypatch):
    """A lone over-budget tool is shown verbatim — never dropped to a bare
    pointer with nothing to point past."""
    import toolsets
    huge = "x" * (TIER1_CATEGORY_TOKEN_BUDGET * 4 + 50)
    monkeypatch.setitem(toolsets.CATEGORIES, "solo",
                        {"panes": [], "commands": [huge], "endpoints": []})
    line = next(l for l in build_tier1_names(["solo"]).splitlines()
                if l.startswith("solo:"))
    assert huge in line
    assert "more (run:" not in line  # nothing omitted, so no pointer
