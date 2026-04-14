"""Tool definitions for native tool-calling LLM support.

Defines pane-operation tools in Anthropic format and provides conversion
functions for OpenAI-compatible APIs plus a unified parser for tool-call
responses from either format.
"""

import json

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic format)
# ---------------------------------------------------------------------------

PANE_TOOLS = [
    {
        "name": "run_command",
        "description": "Run a shell command in the pane. Output will be visible next turn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_screen",
        "description": "Read current terminal screen content from the pane.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of lines to capture (default 50).",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
    {
        "name": "complete",
        "description": "Mark the current task as done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Short summary of what was accomplished.",
                },
            },
            "required": ["summary"],
        },
    },
]


# ---------------------------------------------------------------------------
# Format conversion
# ---------------------------------------------------------------------------

def tools_for_anthropic():
    """Return tool definitions in Anthropic format (as-is)."""
    return PANE_TOOLS


def tools_for_openai():
    """Convert tool definitions to OpenAI function-calling format.

    OpenAI wraps each tool in {"type": "function", "function": {name, description, parameters}}.
    """
    result = []
    for tool in PANE_TOOLS:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        })
    return result


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_tool_calls(raw, format="openai"):
    """Parse tool-call responses into a uniform list.

    Args:
        raw: Raw response data.
            - OpenAI format: list of tool_call objects (from message.tool_calls),
              each with .id, .function.name, .function.arguments (JSON string).
            - Anthropic format: list of content blocks (from response.content),
              which may include text blocks and tool_use blocks.
        format: "openai" or "anthropic".

    Returns:
        List of {"name": str, "args": dict, "id": str}.
    """
    calls = []

    if format == "openai":
        if not raw:
            return calls
        for tc in raw:
            args = tc.function.arguments
            if isinstance(args, str):
                args = json.loads(args)
            calls.append({
                "name": tc.function.name,
                "args": args,
                "id": tc.id,
            })

    elif format == "anthropic":
        if not raw:
            return calls
        for block in raw:
            # Anthropic content blocks: skip text blocks, handle tool_use
            btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if btype != "tool_use":
                continue
            # Support both object-style (SDK) and dict-style
            if isinstance(block, dict):
                calls.append({
                    "name": block["name"],
                    "args": block.get("input", {}),
                    "id": block["id"],
                })
            else:
                calls.append({
                    "name": block.name,
                    "args": getattr(block, "input", {}),
                    "id": block.id,
                })

    return calls
