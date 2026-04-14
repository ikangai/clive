"""Tests for tool-calling support (tool_defs.py + llm.chat_with_tools)."""

import json
import types

import pytest

from tool_defs import PANE_TOOLS, tools_for_openai, tools_for_anthropic, parse_tool_calls


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

class TestPaneTools:
    def test_has_three_tools(self):
        assert len(PANE_TOOLS) == 3

    def test_tool_names(self):
        names = [t["name"] for t in PANE_TOOLS]
        assert names == ["run_command", "read_screen", "complete"]

    def test_schemas_valid(self):
        for tool in PANE_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            schema = tool["input_schema"]
            assert schema["type"] == "object"
            assert "properties" in schema


# ---------------------------------------------------------------------------
# Format conversion
# ---------------------------------------------------------------------------

class TestToolConversion:
    def test_tools_for_anthropic_returns_same(self):
        assert tools_for_anthropic() is PANE_TOOLS

    def test_tools_for_openai_format(self):
        oai = tools_for_openai()
        assert len(oai) == 3
        for entry in oai:
            assert entry["type"] == "function"
            func = entry["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func

    def test_tools_for_openai_preserves_names(self):
        oai = tools_for_openai()
        names = [e["function"]["name"] for e in oai]
        assert names == ["run_command", "read_screen", "complete"]

    def test_tools_for_openai_parameters_match_input_schema(self):
        oai = tools_for_openai()
        for oai_tool, orig_tool in zip(oai, PANE_TOOLS):
            assert oai_tool["function"]["parameters"] == orig_tool["input_schema"]


# ---------------------------------------------------------------------------
# parse_tool_calls — OpenAI format
# ---------------------------------------------------------------------------

def _make_openai_tool_call(id, name, arguments):
    """Create a mock OpenAI tool-call object."""
    func = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(id=id, function=func)


class TestParseOpenAI:
    def test_single_call(self):
        raw = [_make_openai_tool_call("call_1", "run_command", '{"command": "ls -la"}')]
        result = parse_tool_calls(raw, format="openai")
        assert len(result) == 1
        assert result[0]["name"] == "run_command"
        assert result[0]["args"] == {"command": "ls -la"}
        assert result[0]["id"] == "call_1"

    def test_multiple_calls(self):
        raw = [
            _make_openai_tool_call("call_1", "run_command", '{"command": "pwd"}'),
            _make_openai_tool_call("call_2", "read_screen", '{"lines": 30}'),
        ]
        result = parse_tool_calls(raw, format="openai")
        assert len(result) == 2
        assert result[0]["name"] == "run_command"
        assert result[1]["name"] == "read_screen"
        assert result[1]["args"]["lines"] == 30

    def test_complete_tool(self):
        raw = [_make_openai_tool_call("call_3", "complete", '{"summary": "Done with task"}')]
        result = parse_tool_calls(raw, format="openai")
        assert result[0]["name"] == "complete"
        assert result[0]["args"]["summary"] == "Done with task"

    def test_empty_raw(self):
        assert parse_tool_calls(None, format="openai") == []
        assert parse_tool_calls([], format="openai") == []

    def test_dict_arguments_passed_through(self):
        """If arguments is already a dict (some wrappers pre-parse), handle gracefully."""
        raw = [_make_openai_tool_call("call_4", "run_command", {"command": "echo hi"})]
        result = parse_tool_calls(raw, format="openai")
        assert result[0]["args"] == {"command": "echo hi"}


# ---------------------------------------------------------------------------
# parse_tool_calls — Anthropic format
# ---------------------------------------------------------------------------

def _make_anthropic_tool_use(id, name, input_data):
    """Create a mock Anthropic tool_use content block."""
    return types.SimpleNamespace(type="tool_use", id=id, name=name, input=input_data)


def _make_anthropic_text(text):
    return types.SimpleNamespace(type="text", text=text)


class TestParseAnthropic:
    def test_single_call(self):
        raw = [_make_anthropic_tool_use("tu_1", "run_command", {"command": "ls"})]
        result = parse_tool_calls(raw, format="anthropic")
        assert len(result) == 1
        assert result[0]["name"] == "run_command"
        assert result[0]["args"] == {"command": "ls"}
        assert result[0]["id"] == "tu_1"

    def test_skips_text_blocks(self):
        raw = [
            _make_anthropic_text("I'll run the command now."),
            _make_anthropic_tool_use("tu_2", "run_command", {"command": "date"}),
        ]
        result = parse_tool_calls(raw, format="anthropic")
        assert len(result) == 1
        assert result[0]["name"] == "run_command"

    def test_multiple_tool_calls(self):
        raw = [
            _make_anthropic_tool_use("tu_1", "run_command", {"command": "ls"}),
            _make_anthropic_tool_use("tu_2", "complete", {"summary": "all done"}),
        ]
        result = parse_tool_calls(raw, format="anthropic")
        assert len(result) == 2
        assert result[0]["name"] == "run_command"
        assert result[1]["name"] == "complete"

    def test_complete_tool(self):
        raw = [_make_anthropic_tool_use("tu_3", "complete", {"summary": "Finished"})]
        result = parse_tool_calls(raw, format="anthropic")
        assert result[0]["name"] == "complete"
        assert result[0]["args"]["summary"] == "Finished"

    def test_dict_format(self):
        """Handles plain dict blocks (not SDK objects)."""
        raw = [
            {"type": "tool_use", "id": "tu_d", "name": "read_screen", "input": {"lines": 20}},
        ]
        result = parse_tool_calls(raw, format="anthropic")
        assert len(result) == 1
        assert result[0]["name"] == "read_screen"
        assert result[0]["args"] == {"lines": 20}

    def test_empty_raw(self):
        assert parse_tool_calls(None, format="anthropic") == []
        assert parse_tool_calls([], format="anthropic") == []
