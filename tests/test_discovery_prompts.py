"""Tests for discovery.prompts — exploration goal, generation prompt, safety lists."""
import re

from discovery.models import ExplorationResult, ProbeOutcome
from discovery.prompts import (
    CREDENTIAL_TOOLS,
    INTERACTIVE_TOOLS,
    build_exploration_goal,
    build_generation_prompt,
)


def test_exploration_goal_references_tool_name():
    g = build_exploration_goal("rg")
    assert "rg" in g
    assert "--help" in g


def test_exploration_goal_warns_against_destructive_probes():
    g = build_exploration_goal("rg")
    assert re.search(r"do not|DO NOT", g, re.IGNORECASE)
    assert re.search(r"destructive|modify|write", g, re.IGNORECASE)


def test_generation_prompt_includes_driver_template_sections():
    r = ExplorationResult(tool_name="rg", probes=[
        ProbeOutcome(command="rg --help", exit_code=0, screen="ripgrep usage..."),
    ])
    p = build_generation_prompt(r)
    for section in ("ENVIRONMENT", "PRIMARY TOOLS", "PATTERNS", "RESPONSE FORMAT", "COMPLETION"):
        assert section in p
    assert "rg --help" in p
    assert "ripgrep usage" in p


def test_generation_prompt_demands_frontmatter():
    p = build_generation_prompt(ExplorationResult(tool_name="rg"))
    assert "---" in p
    assert "preferred_mode" in p


def test_credential_tools_list_includes_common_offenders():
    # Tools that prompt for credentials must be in CREDENTIAL_TOOLS so
    # explore_tool can force a --help suffix and avoid trapping on
    # a password prompt.
    for tool in ("aws", "gh", "gcloud", "kubectl", "psql", "mysql", "ssh"):
        assert tool in CREDENTIAL_TOOLS


def test_interactive_tools_list_includes_common_tuis():
    for tool in ("vim", "less", "more", "top", "htop", "lazygit", "k9s"):
        assert tool in INTERACTIVE_TOOLS
