"""When CLIVE_PROGRESSIVE_TOOLS=1, build_tools_summary returns Tier0+Tier1."""
import os
import pytest
from toolsets import resolve_toolset, build_tools_summary, check_commands

def _make_inputs(profile="standard"):
    resolved = resolve_toolset(profile)
    available, _ = check_commands(resolved["commands"])
    tool_status = {p["name"]: {"status": "ready",
                                "app_type": p["app_type"],
                                "description": p["description"]}
                   for p in resolved["panes"]}
    return tool_status, available, resolved["endpoints"], resolved["categories"]

def test_progressive_summary_shorter(monkeypatch):
    monkeypatch.delenv("CLIVE_PROGRESSIVE_TOOLS", raising=False)
    ts, ac, ep, cats = _make_inputs("standard")
    legacy = build_tools_summary(ts, ac, ep)

    monkeypatch.setenv("CLIVE_PROGRESSIVE_TOOLS", "1")
    new = build_tools_summary(ts, ac, ep, categories=cats)
    assert len(new) < len(legacy), \
        f"progressive ({len(new)}) should be shorter than legacy ({len(legacy)})"

def test_progressive_default_off(monkeypatch):
    monkeypatch.delenv("CLIVE_PROGRESSIVE_TOOLS", raising=False)
    ts, ac, ep, cats = _make_inputs("standard")
    out = build_tools_summary(ts, ac, ep, categories=cats)
    # Default still emits the legacy format (full descriptions).
    # 'JSON processor' is jq's description text — should appear.
    assert "JSON processor" in out
