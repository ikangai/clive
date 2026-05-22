"""Worker prompt builder should inject Tier-2 cards for subtask.tools."""
import pytest
from models import Subtask
from llm.prompts import build_worker_tool_context  # to be created


def test_worker_tool_context_loads_cards():
    s = Subtask(id="1", description="x", pane="shell", mode="interactive",
                tools=["jq", "rg"])
    block = build_worker_tool_context(s)
    assert "[jq]" in block
    assert "[rg]" in block


def test_worker_tool_context_empty_for_no_tools():
    s = Subtask(id="1", description="x", pane="shell", mode="script")
    assert build_worker_tool_context(s) == ""


def test_worker_tool_context_skips_unknown_tools():
    """A nonexistent tool name in subtask.tools is silently dropped."""
    s = Subtask(id="1", description="x", pane="shell", mode="interactive",
                tools=["jq", "not_a_real_tool"])
    block = build_worker_tool_context(s)
    assert "[jq]" in block
    assert "not_a_real_tool" not in block
