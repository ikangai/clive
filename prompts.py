"""Prompt templates for planner, worker, and summarizer."""

import os

# Path to drivers directory (relative to this file)
_DRIVERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drivers")

DEFAULT_DRIVER = """You control this pane via shell commands.
Read the screen output after each command to decide your next action.
If a command fails, read the error and try a different approach."""


_driver_cache: dict[str, str] = {}
_driver_meta_cache: dict[str, dict] = {}


def _parse_driver_frontmatter(content: str) -> tuple[str, dict]:
    """Split driver content into body and frontmatter metadata.

    Frontmatter is YAML-like between --- markers at the top of the file.
    Returns (body, metadata_dict). If no frontmatter, returns (content, {}).
    """
    if not content.startswith("---"):
        return content, {}
    end = content.find("---", 3)
    if end == -1:
        return content, {}
    front = content[3:end].strip()
    body = content[end + 3:].strip()
    meta = {}
    for line in front.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return body, meta


def load_driver(app_type: str, drivers_dir: str | None = None) -> str:
    """Load a driver prompt for the given app_type.

    Auto-discovers drivers from the drivers/ directory by matching
    {app_type}.md. Falls back to DEFAULT_DRIVER if no file found.
    Caches loaded drivers to avoid repeated disk reads.

    If CLIVE_EVAL_DRIVER_OVERRIDE env var is set to a file path,
    that file is used instead (for eval/evolution overrides).
    """
    override = os.environ.get("CLIVE_EVAL_DRIVER_OVERRIDE")
    if override and os.path.exists(override):
        with open(override, "r") as f:
            return f.read().strip()

    cache_key = f"{app_type}:{drivers_dir or 'default'}"
    if cache_key in _driver_cache:
        return _driver_cache[cache_key]

    base = drivers_dir or _DRIVERS_DIR
    path = os.path.join(base, f"{app_type}.md")
    if os.path.exists(path):
        with open(path, "r") as f:
            raw = f.read().strip()
        body, meta = _parse_driver_frontmatter(raw)
        _driver_cache[cache_key] = body
        _driver_meta_cache[cache_key] = meta
        return body

    _driver_cache[cache_key] = DEFAULT_DRIVER
    _driver_meta_cache[cache_key] = {}
    return DEFAULT_DRIVER


def load_driver_meta(app_type: str, drivers_dir: str | None = None) -> dict:
    """Load driver frontmatter metadata (preferred_mode, use_interactive_when, etc.).

    Calls load_driver() first to ensure cache is populated.
    """
    cache_key = f"{app_type}:{drivers_dir or 'default'}"
    if cache_key not in _driver_meta_cache:
        load_driver(app_type, drivers_dir)
    return _driver_meta_cache.get(cache_key, {})


def build_planner_prompt(tools_summary: str) -> str:
    from skills import skills_summary_for_planner
    skills_info = skills_summary_for_planner()

    return f"""You are a task planner for an autonomous terminal agent.

The agent controls CLI tools via tmux panes. Each pane is a terminal conversation: the agent reads the screen, reasons, and types commands. This is the universal interface — every tool interaction flows through a pane.

{tools_summary}
{skills_info}

Each subtask targets exactly one PANE. COMMANDS and APIS run inside panes — use them freely in subtask descriptions (e.g. "use jq to parse the API response", "curl wttr.in for weather").

RULES:
1. Each subtask must target exactly one pane.
2. Subtasks on DIFFERENT panes CAN run in parallel (if no data dependency).
3. Subtasks on the SAME pane MUST be sequential — add depends_on to enforce order.
4. Keep subtasks at goal-level granularity: "fetch the page and extract links" not "run curl".
5. A worker can execute multiple commands to achieve its subtask goal.
6. Minimize the number of subtasks — prefer fewer, broader subtasks over many tiny ones.
7. Only create dependencies where there is a genuine data flow or ordering requirement.
8. Workers can share data by writing files to the session working directory.
9. When a task needs COMMANDS or APIS, route to a shell-type pane (shell, browser, data, docs all work).
10. Each subtask has a "mode" — this controls how much the agent observes during execution:
    - "script": One-shot. The agent generates a shell script, executes it, checks the exit code. No observation during execution. Use for: deterministic pipelines, file operations, data extraction, known API calls, text processing. Faster and cheaper.
    - "planned": Multi-step mechanical. The agent generates a sequence of commands with verification criteria, then executes them one-by-one without further LLM calls. Use for: deterministic multi-step workflows where each step is a known command — install+configure, fetch+process+save, multi-file operations. Even cheaper than script for multi-step tasks.
    - "interactive": Turn-by-turn. The agent reads the screen after each command and decides what to do next. Use for: exploring unknown content, debugging, multi-step workflows where the next step depends on the previous result, interactive applications.
    - "streaming": Like interactive, but with automatic intervention detection. The agent is alerted when the process prompts for input (passwords, confirmations, [y/N] prompts). Use for: package installs that may ask for confirmation, operations requiring passwords, long-running processes that may prompt for input, interactive debuggers.
    Each pane above declares [prefer: mode] — follow it. The principle: if the next step does NOT depend on seeing the previous result, use "script". Use "streaming" only when the process may prompt for passwords or confirmations.
11. Each subtask can optionally declare "produces" (filename it will write to the session dir) and "expects" (files it needs from dependencies). This helps downstream subtasks know exactly what data is available.

Respond with a JSON object and nothing else:
{{
  "subtasks": [
    {{
      "id": "1",
      "description": "Analyze the syslog for errors and patterns",
      "pane": "shell",
      "mode": "script",
      "skill": "analyze-logs",
      "produces": "errors.txt",
      "depends_on": []
    }},
    {{
      "id": "2",
      "description": "Browse the documentation site and find the configuration reference",
      "pane": "browser",
      "mode": "interactive",
      "produces": "config_ref.txt",
      "depends_on": []
    }},
    {{
      "id": "3",
      "description": "Summarize the errors using the config context",
      "pane": "shell",
      "mode": "script",
      "expects": ["errors.txt", "config_ref.txt"],
      "depends_on": ["1", "2"]
    }}
  ]
}}
"""


def build_classifier_prompt(
    available_panes: list[str],
    installed_commands: list[str],
    missing_commands: list[str],
    available_endpoints: list[str],
    unconfigured_tools: list[str] | None = None,
) -> str:
    """Build the Tier 1 fast classifier system prompt."""
    return f"""You are a fast intent classifier for a CLI automation agent.

Given a user's task, classify it and route to the right execution mode.

Available panes: {', '.join(available_panes) if available_panes else 'shell'}
Installed commands: {', '.join(installed_commands) if installed_commands else 'basic shell'}
Missing commands: {', '.join(missing_commands) if missing_commands else 'none'}
Unconfigured tools (installed, need setup first): {', '.join(unconfigured_tools) if unconfigured_tools else 'none'}
Available APIs: {', '.join(available_endpoints) if available_endpoints else 'none'}

Respond with ONLY valid JSON (no markdown, no explanation):

{{
  "mode": "direct|script|interactive|plan|unavailable|unconfigured|answer|clarify",
  "tool": "primary tool name or null",
  "pane": "target pane name (usually shell)",
  "driver": "driver name (shell, browser, email_cli, data, docs, media, or null)",
  "cmd": "exact shell command to run (for mode=direct only, else null)",
  "fallback_mode": "script|interactive|null (fallback if direct fails)",
  "stateful": true/false,
  "message": "explanation (for unavailable/unconfigured/answer/clarify modes, else null)"
}}

Mode guide:
- "direct": task IS a shell command or maps to a single known command. Provide exact cmd.
- "script": task needs a short script (file processing, data transformation, multi-step shell).
- "interactive": task needs a TUI app (mutt, lynx) or multi-turn exploration.
- "plan": complex multi-step task needing parallel subtasks or multiple tools.
- "unavailable": required tool is in the missing_commands list. Include install hint in message.
- "unconfigured": tool is installed but needs account/credential setup. Set tool name.
- "answer": question about the system, no execution needed. Put answer in message.
- "clarify": task is too vague. Put clarifying question in message.

Examples:
- "curl ikangai.com" -> {{"mode":"direct","tool":"curl","pane":"shell","driver":"shell","cmd":"curl -sL ikangai.com","fallback_mode":"script","stateful":false,"message":null}}
- "count .py files and write a haiku" -> {{"mode":"script","tool":"shell","pane":"shell","driver":"shell","cmd":null,"fallback_mode":null,"stateful":true,"message":null}}
- "send email to bob@x.com" (mutt missing) -> {{"mode":"unavailable","tool":"mutt","pane":"email","driver":"email_cli","cmd":null,"fallback_mode":null,"stateful":false,"message":"Email requires neomutt. Install: brew install neomutt"}}
- "send email to bob@x.com" (email unconfigured) -> {{"mode":"unconfigured","tool":"email","pane":"email","driver":"email_cli","cmd":null,"fallback_mode":null,"stateful":false,"message":"Email needs account setup"}}
- "ls -la | grep .py" -> {{"mode":"direct","tool":"ls","pane":"shell","driver":"shell","cmd":"ls -la | grep .py","fallback_mode":null,"stateful":false,"message":null}}
- "read my latest 5 emails" -> {{"mode":"script","tool":"email","pane":"email","driver":"email_cli","cmd":null,"fallback_mode":"interactive","stateful":true,"message":null}}
- "scrape 5 sites and compare them" -> {{"mode":"plan","tool":null,"pane":null,"driver":null,"cmd":null,"fallback_mode":null,"stateful":true,"message":null}}
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


def build_summarizer_prompt(output_format: str = "default") -> str:
    base = """You are summarizing the results of a multi-step task execution.

Given the original task and the results from each subtask, provide:
1. A concise summary of what was accomplished
2. Any notable findings or outputs
3. Any subtasks that failed or were skipped, and why

Be concise and factual."""

    if output_format == "oneline":
        return base + "\n\nIMPORTANT: Respond with a SINGLE LINE. No newlines, no formatting, no bullet points. One sentence."
    elif output_format == "json":
        return base + '\n\nIMPORTANT: Respond with a JSON object ONLY: {"result": "summary text", "status": "success"|"partial"|"error", "details": [{"subtask": "id", "status": "...", "summary": "..."}]}'
    elif output_format == "bool":
        return base + "\n\nIMPORTANT: Respond with exactly YES or NO. Nothing else. YES means the task was fully accomplished. NO means it was not."
    return base


def build_script_prompt(
    subtask_description: str,
    pane_name: str,
    app_type: str,
    tool_description: str,
    dependency_context: str,
    session_dir: str = "/tmp/clive",
) -> str:
    """Script prompt — the core. A professional writes a script, runs it once."""
    dep_section = ""
    if dependency_context:
        dep_section = f"""
Context from prior steps:
{dependency_context}
"""

    driver = load_driver(app_type)

    import platform
    os_name = platform.system()
    os_arch = platform.machine()
    os_info = f"OS: {os_name} ({os_arch})"
    if os_name == "Darwin":
        os_info += "\nIMPORTANT: macOS with BSD coreutils. Use POSIX-compatible commands."

    return f"""You are a skilled engineer writing a script for: {subtask_description}

Pane: {pane_name} [{app_type}] — {tool_description}
{os_info}

{driver}
{dep_section}
Write a single self-contained script. Choose bash or Python — whichever fits best.
- Bash: start with #!/bin/bash and use set -euo pipefail
- Python: start with #!/usr/bin/env python3
- Read input from the current working directory (relative paths)
- Write output/results to {session_dir}/ (absolute paths)
- Print a short preview of output + one-line summary as last line

Respond with ONLY the script in a fenced code block. No prose.
"""


def build_planned_prompt(
    subtask_description: str,
    pane_name: str,
    app_type: str,
    tool_description: str,
    dependency_context: str,
    session_dir: str = "/tmp/clive",
) -> str:
    """Planned prompt — LLM generates a full step-by-step plan with verification in ONE call."""
    dep_section = ""
    if dependency_context:
        dep_section = f"""
Context from prior steps:
{dependency_context}
"""

    driver = load_driver(app_type)

    import platform
    os_name = platform.system()
    os_arch = platform.machine()
    os_info = f"OS: {os_name} ({os_arch})"
    if os_name == "Darwin":
        os_info += "\nIMPORTANT: macOS with BSD coreutils. Use POSIX-compatible commands."

    return f"""You are a skilled engineer planning a sequence of shell commands for: {subtask_description}

Pane: {pane_name} [{app_type}] — {tool_description}
{os_info}

{driver}
{dep_section}
Generate a step-by-step plan as a JSON object. Each step is one shell command.
The harness will execute each step sequentially, check the exit code, and handle failures.

- Each step has a "cmd" (shell command), "verify" (currently always "exit_code == 0"), and "on_fail" action.
- on_fail options: "retry" (re-run the command once), "skip" (continue to next step), "abort" (stop execution).
- Use "abort" for critical steps, "skip" for optional steps, "retry" for flaky operations.
- Write output/results to {session_dir}/ (absolute paths).
- Keep commands simple — one logical operation per step.

Respond with ONLY a JSON object (no prose, no markdown):
{{
  "steps": [
    {{"cmd": "command here", "verify": "exit_code == 0", "on_fail": "abort"}},
    {{"cmd": "another command", "verify": "exit_code == 0", "on_fail": "skip"}}
  ],
  "done_summary": "one-line summary of what the plan accomplishes"
}}
"""


def build_interactive_prompt(
    subtask_description: str,
    pane_name: str,
    app_type: str,
    tool_description: str,
    dependency_context: str,
    session_dir: str = "/tmp/clive",
) -> str:
    """Interactive prompt — the exception. For when you need to see before you act."""
    dep_section = ""
    if dependency_context:
        dep_section = f"""
Prior results:
{dependency_context}
"""

    driver = load_driver(app_type)

    return f"""You control pane "{pane_name}" [{app_type}] — {tool_description}

{driver}

GOAL: {subtask_description}
{dep_section}
You're investigating something where the next step depends on what you see.
Each turn: put your command in a ```bash code block. Read the screen output (shown next turn). Decide what's next.
Write results to {session_dir}/
When done: DONE: <one-line summary>
"""
