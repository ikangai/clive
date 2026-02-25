# agent-cli

An LLM agent that drives CLI tools through tmux. It reads the terminal screen as input and sends keystrokes as output — giving a language model direct control over shell sessions, browsers, email clients, and any other terminal program.

## How it works

```
┌─────────────┐     screen capture      ┌───────────┐
│  tmux panes  │ ─────────────────────► │           │
│              │                         │  LLM API  │
│  shell       │ ◄───────────────────── │           │
│  browser     │     keystrokes          └───────────┘
│  email       │
└─────────────┘
```

Each tool runs in its own tmux pane. Every turn, the agent:

1. Captures the screen content from all panes
2. Sends the screens + conversation history to the LLM
3. Parses the LLM's response for a command
4. Executes the command in the target pane
5. Waits for output to settle, then repeats

The agent can read/write files directly, and declares the task complete when done.

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

Tools are defined in the `DEFAULT_TOOLS` list inside `agent.py`. Each tool gets its own tmux pane:

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
| `MODEL` | `z-ai/glm-5` | OpenRouter model ID |
| `IDLE_TIMEOUT` | `2.0` | Seconds to wait for pane output to settle |
| `MAX_TURNS` | `50` | Maximum agent turns before stopping |

## Project structure

```
agent.py          — agent loop and entry point
fetch_emails.sh   — IMAP email fetcher (used by the email tool)
send_reply.sh     — email sender via msmtp
requirements.txt  — Python dependencies
.env              — API keys (not committed)
```
