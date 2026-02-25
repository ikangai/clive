# agent-cli

An LLM agent that drives CLI tools through tmux. It reads the terminal screen as input and sends keystrokes as output — giving a language model direct control over shell sessions, browsers, email clients, and any other terminal program.

## How it works

```
                         ┌──────────┐
                         │ Planner  │  LLM decomposes task into subtask DAG
                         └────┬─────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              ┌──────────┐       ┌──────────┐
              │ Worker 1 │       │ Worker 2 │  parallel on different panes
              │ (shell)  │       │ (browser)│
              └────┬─────┘       └────┬─────┘
                   │                   │
                   └─────────┬─────────┘
                             ▼
                       ┌──────────┐
                       │ Worker 3 │  waits for 1+2 (dependency)
                       │ (shell)  │
                       └────┬─────┘
                            ▼
                      ┌───────────┐
                      │ Summarizer│  synthesizes all results
                      └───────────┘
```

The agent runs in three phases:

1. **Plan** — The LLM decomposes your task into subtasks with dependencies, forming a DAG
2. **Execute** — Independent subtasks run in parallel on different tmux panes; dependent subtasks wait for their prerequisites
3. **Summarize** — Results from all subtasks are synthesized into a final report

Each subtask worker has its own LLM conversation and controls exactly one pane via screen capture (input) and keystrokes (output).

## Prerequisites

- **tmux** — `brew install tmux` or `apt install tmux`
- **Python 3.10+**
- **OpenRouter API key** — get one at [openrouter.ai](https://openrouter.ai)
- **lynx** (optional, for the browser tool) — `brew install lynx`

## Quickstart

```bash
git clone <repo-url> && cd agent-cli

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create a .env file with your API key
echo "OPENROUTER_API_KEY=sk-or-..." > .env

# Run the agent
python agent.py "list all files in /tmp and summarize what you find"
```

Watch the agent work in real-time:

```bash
tmux attach -t agent
```

## Usage

```bash
# Run with a task
python agent.py "your task description here"

# Run the built-in example task
python agent.py

# Show help
python agent.py --help
```

## Configuring tools

Tools are defined in the `DEFAULT_TOOLS` list inside `session.py`. Each tool gets its own tmux pane:

```python
{
    "name": "shell",       # pane identifier
    "cmd": None,           # command to run at startup (None = plain shell)
    "app_type": "shell",   # metadata tag for the LLM
    "description": "...",  # tells the LLM what this tool does
    "host": None           # SSH host for remote tools (None = local)
}
```

To add a tool, append an entry to `DEFAULT_TOOLS`. To run a tool on a remote machine, set `host` to an SSH target like `user@server.example.com`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `AGENT_MODEL` | `z-ai/glm-5` | OpenRouter model ID (env var or in `llm.py`) |
| `idle_timeout` | `2.0` | Per-tool idle timeout in seconds (in tool config) |
| `max_turns` | `15` | Per-subtask turn budget (in `models.py`) |

## Project structure

```
agent.py          — orchestrator: plan → execute → summarize
planner.py        — LLM decomposes task into subtask DAG (JSON)
executor.py       — DAG scheduler + per-subtask worker loops
session.py        — tmux session/pane management + tool registry
models.py         — dataclasses: Subtask, Plan, SubtaskResult, PaneInfo
llm.py            — shared OpenAI/OpenRouter client
prompts.py        — all LLM prompt templates
completion.py     — three-strategy completion detection (marker/prompt/idle)
fetch_emails.sh   — IMAP email fetcher (used by the email tool)
send_reply.sh     — email sender via msmtp
requirements.txt  — Python dependencies
.env              — API keys (not committed)
```
