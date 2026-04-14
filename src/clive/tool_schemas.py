"""Tool schemas for structured LLM tool_use (Anthropic/OpenAI function calling).

These define the same commands currently encoded as XML in the worker prompt,
but in a structured format that can be passed to tool_use-capable providers.
This eliminates XML parsing and reduces per-turn token overhead by ~150 tokens.

Usage (future): pass WORKER_TOOLS to the LLM alongside the system prompt.
The LLM returns tool_use blocks instead of XML text, which are parsed directly.
"""

WORKER_TOOLS = [
    {
        "name": "shell",
        "description": "Execute a shell command in the pane. One command per turn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to read",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file on the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to write to",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "wait",
        "description": "Wait for a specified number of seconds before re-observing the screen.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": "Seconds to wait (1-10)",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "task_complete",
        "description": "Signal that the task goal has been achieved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Summary of what was accomplished",
                },
            },
            "required": ["summary"],
        },
    },
]
