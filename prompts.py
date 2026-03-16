"""Prompt templates for planner, worker, and summarizer."""

import os

# Path to drivers directory (relative to this file)
_DRIVERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drivers")

DEFAULT_DRIVER = """You control this pane via shell commands.
Read the screen output after each command to decide your next action.
If a command fails, read the error and try a different approach."""


def load_driver(app_type: str, drivers_dir: str | None = None) -> str:
    """Load a driver prompt for the given app_type.

    Auto-discovers drivers from the drivers/ directory by matching
    {app_type}.md. Falls back to DEFAULT_DRIVER if no file found.
    """
    base = drivers_dir or _DRIVERS_DIR
    path = os.path.join(base, f"{app_type}.md")
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    return DEFAULT_DRIVER


def build_planner_prompt(tools_summary: str) -> str:
    return f"""You are a task planner for an autonomous terminal agent.

The agent controls CLI tools via tmux panes. Each pane is a terminal conversation: the agent reads the screen, reasons, and types commands. This is the universal interface — every tool interaction flows through a pane.

{tools_summary}

Each subtask targets exactly one PANE. COMMANDS and APIS run inside panes — use them freely in subtask descriptions (e.g. "use jq to parse the API response", "curl wttr.in for weather").

RULES:
1. Each subtask must target exactly one pane.
2. Subtasks on DIFFERENT panes CAN run in parallel (if no data dependency).
3. Subtasks on the SAME pane MUST be sequential — add depends_on to enforce order.
4. Keep subtasks at goal-level granularity: "fetch the page and extract links" not "run curl".
5. A worker can execute multiple commands to achieve its subtask goal.
6. Minimize the number of subtasks — prefer fewer, broader subtasks over many tiny ones.
7. Only create dependencies where there is a genuine data flow or ordering requirement.
8. Workers can share data by writing files to /tmp/clive/.
9. When a task needs COMMANDS or APIS, route to a shell-type pane (shell, browser, data, docs all work).

Respond with a JSON object and nothing else:
{{
  "subtasks": [
    {{
      "id": "1",
      "description": "Clear description of what this subtask should accomplish",
      "pane": "shell",
      "depends_on": []
    }},
    {{
      "id": "2",
      "description": "Another subtask that can run in parallel on a different pane",
      "pane": "browser",
      "depends_on": []
    }},
    {{
      "id": "3",
      "description": "A subtask that needs results from 1 and 2",
      "pane": "shell",
      "depends_on": ["1", "2"]
    }}
  ]
}}
"""


def build_worker_prompt(
    subtask_description: str,
    pane_name: str,
    app_type: str,
    tool_description: str,
    dependency_context: str,
) -> str:
    dep_section = ""
    if dependency_context:
        dep_section = f"""
Results from prerequisite tasks (use this information):
{dependency_context}
"""

    driver = load_driver(app_type)

    return f"""You are an autonomous agent worker controlling a single tmux pane.

Your pane: {pane_name} [{app_type}] — {tool_description}

Tool knowledge:
{driver}

Your goal:
{subtask_description}
{dep_section}
Send exactly one command per turn using XML tags:

  <cmd type="shell" pane="{pane_name}">your command here</cmd>
  <cmd type="read_file" pane="{pane_name}">/path/to/file</cmd>
  <cmd type="write_file" pane="{pane_name}" path="/path/to/file">content</cmd>
  <cmd type="task_complete">summary of what you accomplished</cmd>

Rules:
- One command per turn.
- You can ONLY send commands to pane "{pane_name}".
- Use task_complete when your goal is achieved.
- Write intermediate results to /tmp/clive/ so other tasks can use them.
- read_file and write_file operate on the LOCAL filesystem only. For remote panes, use cat/shell redirects instead.
- If something unexpected happens, describe it in your response and try to recover.
- Silent commands (mkdir, touch) produce no output — this is normal.
"""


def build_triage_prompt(clive_context: str) -> str:
    return f"""You are a task triage agent for clive (CLI Live Environment).

clive is a Python-based LLM agent that drives CLI tools through tmux panes.
The LLM reads the terminal screen, reasons about what it sees, and types commands.
No structured APIs needed — the pane IS the interface.

{clive_context}

When the user sends a message, classify it and respond with a JSON object:

1. If it's a question about clive itself (setup, config, usage, profiles, tools, what it can do):
   {{"action": "answer", "response": "Your helpful answer based on the context above"}}

2. If the task is too vague or ambiguous to execute — you need specifics like which files, what format, which account:
   {{"action": "clarify", "question": "Your specific clarifying question"}}

3. If the task is clear enough to execute:
   {{"action": "execute", "task": "Refined task description if needed, or the original"}}

Guidelines:
- Prefer "execute" when the task is reasonably clear, even if imperfect. Don't over-ask.
- Only "clarify" when missing critical information that would cause the task to fail.
- For "answer", ONLY use information from the context above. Never hallucinate features.
- If you don't know the answer, say so and suggest the user check /help or TOOLS.md.
- Respond with only the JSON object, nothing else."""


def build_summarizer_prompt() -> str:
    return """You are summarizing the results of a multi-step task execution.

Given the original task and the results from each subtask, provide:
1. A concise summary of what was accomplished
2. Any notable findings or outputs
3. Any subtasks that failed or were skipped, and why

Be concise and factual."""
