# agent-cli

An LLM agent that drives CLI tools through tmux. It reads the terminal screen as input and sends keystrokes as output — giving a language model direct control over shell sessions, browsers, email clients, and any other terminal program.

## Why this exists

Most agent infrastructure asks: *how do we give agents access to our systems?* The answer is usually APIs and protocols — structured, stateless, deterministic. agent-cli asks a different question: *what kind of environment do agents naturally thrive in?*

The answer is the terminal. Not as a retro curiosity, but because it's already an **agent habitat** — a persistent, stateful, observable space where things happen over time and an agent can act inside it.

The distinction matters. An API is a call-response primitive. An environment is a thing you inhabit. The shell has always been an environment: you open it, things happen, you respond, state accumulates, you leave it in a different condition than you found it. That's not what APIs do.

This isn't an API replacement. It's not MCP (a protocol for exposing tools). It's an **environment interface** — the agent doesn't call the shell, it *lives in* it. It reads what's on screen, types keystrokes, watches what happens. No schemas, no tool definitions, no structured calls. Just a screen and a keyboard, like the rest of us.

The terminal turns out to be accidentally well-designed for agents:

- **Observable state** — screen content is the agent's perception
- **Action space** — keystrokes are the agent's motor output
- **Persistent context** — working directory, env vars, running processes
- **Composable tools** — pipes, files, scripts, fifty years of them
- **Natural boundaries** — sessions and SSH as membranes between habitats

The file system becomes shared memory between subtasks. The panes become rooms the agent works in. The tmux session is the space the agent inhabits for the duration of a task.

There's been a quiet movement where everything became an API, everything became stateless, everything became a structured call. We lost the environment. **CLIfication** is the reversal: bring back the environment, the stream, the persistent stateful workspace — specifically for agents that navigate the world by observing and acting, not by making function calls.

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

## Remote habitats

SSH is the inter-habitat protocol. It gives you everything you'd design from scratch — authentication, authorization, encryption, auditability, revocability — for free. No new protocol, no token management, no new security surface.

### Basic case

Add a remote tool in `session.py`. The agent drives it exactly like a local pane:

```python
{
    "name": "build_server",
    "cmd": "ssh deploy@build.example.com",
    "app_type": "shell",
    "description": "Build server — run tests, compile, check logs",
    "host": "deploy@build.example.com",
}
```

The `host` field tells the setup code this is remote — it connects first, then configures the environment on the remote shell. The agent never knows the difference between local and remote panes.

### ControlMaster — important for agents

Opening a new SSH connection per pane is slow. Use multiplexing:

```bash
# ~/.ssh/config
Host build.example.com
  ControlMaster    auto
  ControlPath      ~/.ssh/cm-%r@%h:%p
  ControlPersist   10m
  User             deploy
  IdentityFile     ~/.ssh/agent_key
```

First connection opens the tunnel, subsequent ones reuse it instantly.

### Dedicated agent key

Don't reuse your personal SSH key. Create one for the agent:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/agent_key -C "agent-cli"
ssh-copy-id -i ~/.ssh/agent_key.pub deploy@build.example.com
```

Revoke agent access without touching your own keys. If the agent does something unexpected, pull the key.

### Habitat topology

```
local machine
  └── tmux session "agent"
        ├── pane: shell          (local)
        ├── pane: browser        (local lynx)
        ├── pane: build_server   (ssh → build.example.com)
        ├── pane: staging        (ssh → staging.example.com)
        └── pane: remote_agent   (ssh → agents.example.com → another agent-cli)
```

Each pane is a room. SSH is the door between buildings. The agent navigates between them by targeting the right pane name in its command.

### Agent-to-agent

The most interesting topology: one agent driving a pane that contains another agent. The outer agent sends natural language, the inner agent executes in its own habitat and reports back through stdout.

```python
{
    "name": "remote_agent",
    "cmd": "ssh deploy@agents.example.com 'python agent.py'",
    "app_type": "agent",
    "description": "Remote agent. Send tasks as plain text, read results from screen.",
    "host": "deploy@agents.example.com",
    "connect_timeout": 5,
}
```

### Long-running disconnected tasks

If your local machine sleeps, the SSH session drops. For tasks that run overnight, start the agent on the remote host inside its own tmux session:

```bash
# start agent on remote, detached
ssh build.example.com 'tmux new-session -d -s agent "python agent.py \"your task\""'

# check in later
ssh build.example.com 'tmux attach -t agent'
```

The habitat persists on the remote machine. You just visit it.

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
