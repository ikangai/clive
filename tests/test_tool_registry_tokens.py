"""Token-budget regression for tool registry tiers (gh#39)."""
import pytest
from toolsets import resolve_toolset, build_tools_summary, check_commands

# Cheap token estimator: 4 chars per token is a stable rule-of-thumb
# within ±10% for English-y prompts. Avoids pulling tiktoken in tests.
def _approx_tokens(text: str) -> int:
    return len(text) // 4

def test_baseline_full_toolset_tokens():
    """Capture today's cost: full profile, all tools dumped."""
    resolved = resolve_toolset("full")
    available, _ = check_commands(resolved["commands"])
    # tool_status mimic: every pane "ready"
    tool_status = {p["name"]: {"status": "ready",
                                "app_type": p["app_type"],
                                "description": p["description"]}
                   for p in resolved["panes"]}
    summary = build_tools_summary(tool_status, available, resolved["endpoints"])
    tokens = _approx_tokens(summary)
    # Tripwire: today the full profile is ~600-900 tokens. If it ever
    # crosses 1500 something is wrong with how summaries grow.
    assert tokens < 1500, f"full toolset summary now {tokens} tokens"
    # And document the floor: ensures the test runs against real data.
    assert tokens > 200, f"summary suspiciously small: {tokens} tokens"
