# clive

**CLI Live Environment** — an LLM agent that drives CLI tools through tmux. It reads the terminal screen as input and sends keystrokes as output — giving a language model direct control over shell sessions, browsers, email clients, and any other terminal program.

## Why this exists

Most agent infrastructure asks: *how do we give agents access to our systems?* The answer is usually APIs and protocols — structured, stateless, deterministic. clive asks a different question: *what kind of environment do agents naturally thrive in?*

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

### Architecture

```
                              ┌──────────────────────┐
                              │         LLM          │
                              └───────────┬──────────┘
                                          │
                            screen ◄──────┴──────► keystrokes
                                          │
                              ┌───────────▼──────────┐
                              │   SESSION MANAGER    │
                              │      clive.py        │
                              └──────┬───────────────┘
                                     │
                     ┌───────────────┴────────────────────┐
                     │                                    │
                     ▼                                    ▼  SSH
          ┌─────────────────────┐             ┌─────────────────────┐
          │    LOCAL SESSION    │             │   REMOTE SESSION    │
          │                     │             │                     │
          │  ┌───────────────┐  │             │  ┌───────────────┐  │
          │  │     tmux      │  │             │  │     tmux      │  │
          │  ├───────────────┤  │             │  ├───────────────┤  │
          │  │ pane: shell   │  │             │  │ pane: browser │  │
          │  │ pane: email   │  │             │  │ pane: files   │  │
          │  │ pane: ...     │  │             │  │ pane: ...     │  │
          │  └───────┬───────┘  │             │  └───────┬───────┘  │
          │          │          │             │          │          │
          │    text  │  keys    │             │    text  │  keys    │
          │          ▼          │             │          ▼          │
          │  ┌───────────────┐  │             │  ┌───────────────┐  │
          │  │  CLI TOOLS    │  │             │  │  CLI TOOLS    │  │
          │  │               │  │             │  │               │  │
          │  │  lynx         │  │             │  │  lynx / w3m   │  │
          │  │  curl         │  │             │  │  grep / head  │  │
          │  │  mutt         │  │             │  │  tee / ls     │  │
          │  │  icalBuddy    │  │             │  └───────┬───────┘  │
          │  │  rg           │  │             │          │          │
          │  └───────┬───────┘  │             │   ~/files/          │
          │          │          │             │  ┌───────────────┐  │
          └──────────│──────────┘             │  │  shared files │  │
                     │                        │  │  channel      │◄─┼── scp
                     │                        │  └───────────────┘  │
                     │                        └─────────────────────┘
                     │
                     ▼
          ┌─────────────────────┐
          │      SERVICES       │
          │  email · calendar   │
          │  web · files · ...  │
          └─────────────────────┘
```

## Prerequisites

- **tmux** — `brew install tmux` or `apt install tmux`
- **Python 3.10+**
- **An LLM provider** — OpenRouter (default), Anthropic, OpenAI, Google Gemini, LMStudio, or Ollama
- **lynx** (optional, for the browser tool) — `brew install lynx`

## Quickstart

```bash
git clone <repo-url> && cd clive

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create a .env file (see .env.example for all providers)
cp .env.example .env
# Edit .env with your provider and API key

# Run with the minimal toolset (default — just a shell, zero install)
python clive.py "list all files in /tmp and summarize what you find"

# Use the standard toolset for web browsing and data processing
python clive.py -t standard "browse example.com and summarize it"

# See all available toolsets
python clive.py --list-toolsets
```

Watch the agent work in real-time:

```bash
tmux attach -t clive
```

## Usage

```bash
# Run with a task
python clive.py "your task description here"

# Select a toolset profile
python clive.py -t standard "your task"
python clive.py --toolset full "your task"

# List available toolsets
python clive.py --list-toolsets

# Run the built-in example task
python clive.py

# Show help
python clive.py --help
```

## Toolsets

Tools are organized into **profiles** in `toolsets.py`. Each profile is a curated set of tmux panes:

| Profile | Panes | Use case |
|---|---|---|
| `minimal` | shell | Zero install, filesystem tasks |
| `standard` | shell, browser, data, docs | Research and data processing |
| `full` | standard + email, calendar, tasks, media | Full productivity |
| `remote` | shell, remote browser, remote files, email | Remote server work |

See [TOOLS.md](TOOLS.md) for the full catalog, install instructions, and how to create custom profiles.

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
ssh-keygen -t ed25519 -f ~/.ssh/agent_key -C "clive"
ssh-copy-id -i ~/.ssh/agent_key.pub deploy@build.example.com
```

Revoke agent access without touching your own keys. If the agent does something unexpected, pull the key.

### Habitat topology

```
local machine
  └── tmux session "clive"
        ├── pane: shell          (local)
        ├── pane: browser        (local lynx)
        ├── pane: build_server   (ssh → build.example.com)
        ├── pane: staging        (ssh → staging.example.com)
        └── pane: remote_agent   (ssh → agents.example.com → another clive)
```

Each pane is a room. SSH is the door between buildings. The agent navigates between them by targeting the right pane name in its command.

### Agent-to-agent

The most interesting topology: one agent driving a pane that contains another agent. The outer agent sends natural language, the inner agent executes in its own habitat and reports back through stdout.

```python
{
    "name": "remote_agent",
    "cmd": "ssh deploy@agents.example.com 'python clive.py'",
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
ssh build.example.com 'tmux new-session -d -s clive "python clive.py \"your task\""'

# check in later
ssh build.example.com 'tmux attach -t clive'
```

The habitat persists on the remote machine. You just visit it.

## Securing remote habitats

The security objections to giving an agent SSH access are valid, but each has a standard Unix answer. The model is defense in depth: restricted shell, SSH constraints, filesystem isolation, and container boundaries — layered so that no single failure grants broad access.

### Layer 1: restricted shell + dedicated user

Create a user whose shell is `rbash` (restricted bash). The agent SSHs in as that user and can only run commands you've explicitly permitted.

```bash
useradd --create-home --shell /bin/rbash agent_email
```

`rbash` blocks: changing directories with `cd`, setting `PATH`, redirecting output with `>`, executing commands with `/`. Then populate the user's `PATH` with only allowed commands:

```bash
# /home/agent_email/.bashrc
PATH=/home/agent_email/bin
readonly PATH

# /home/agent_email/bin/ contains only what you allow:
ln -s /usr/bin/fetch_emails   /home/agent_email/bin/
ln -s /usr/bin/send_reply     /home/agent_email/bin/
# that's it — no ls, no cat, no curl
```

### Layer 2: restrict SSH itself

In `authorized_keys`, constrain what a specific key can do at the SSH level, before the shell even starts:

```
# ~/.ssh/authorized_keys on the server
restrict,command="/home/agent_email/bin/fetch_emails" ssh-ed25519 AAAA...
```

`restrict` blocks port forwarding, X11, agent forwarding, and PTY allocation. `command=` means this key can only run that one command regardless of what the client requests.

For an agent that needs interactive access but constrained:

```
restrict,pty ssh-ed25519 AAAA...
```

Allows a terminal, blocks everything else.

### Layer 3: filesystem isolation

chroot jails the user into a subdirectory — they cannot see anything outside it:

```bash
chroot /jail/agent_email /bin/rbash
```

Setting up a chroot takes work (copy binaries and their dependencies) but it's the strongest isolation short of a container.

### Layer 4: just use a container

For a "service as SSH shell" model, a container per service is cleaner than chroot:

```dockerfile
FROM alpine:latest
RUN adduser -D -s /bin/sh agent
COPY fetch_emails.sh /usr/local/bin/fetch_emails
COPY send_reply.sh /usr/local/bin/send_reply
RUN chmod +x /usr/local/bin/*

RUN apk add openssh
COPY authorized_keys /home/agent/.ssh/authorized_keys
```

The "service" is a container that accepts SSH and exposes exactly two commands. If something goes wrong, delete the container.

### The service provider model

Services as SSH shells — each service is a container with an SSH server and a constrained set of CLI tools:

```
agents.example.com
  └── port 2201  →  container: email_service
  └── port 2202  →  container: calendar_service
  └── port 2203  →  container: crm_service
```

Clients get a key per service. Provision by spinning up a container, revoke by removing the key or killing the container. The agent's tool config:

```python
{
    "name": "email",
    "cmd": "ssh -p 2201 -i ~/.ssh/agent_email agent@agents.example.com",
    "app_type": "email_cli",
    "description": "Managed email service. fetch_emails, send_reply, search_mail available.",
    "host": "agent@agents.example.com",
}
```

### Security objections, answered

| Objection | Answer |
|---|---|
| Agent could escalate privileges | Restricted shell, no sudo, no setuid binaries, no PATH manipulation |
| Agent could exfiltrate data | Outbound network rules on the container — it talks to your mail server and nowhere else |
| Agent could fill the disk | Disk quotas on the user or container storage limits |
| Compromised key gives full access | Key is scoped to one container, one service. Blast radius is bounded |
| Can't audit what happened | `script` command or shell logging captures everything. SSH logs the session. Container logs capture all output |

### The honest remaining risk

The weakest point isn't the shell or the container — it's the CLI tools themselves. If `fetch_emails` has a bug that allows command injection through a crafted email subject line, the jail doesn't help. The tools inside the container need to be written defensively. That's the actual security surface.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `openrouter` | LLM provider: `openai`, `anthropic`, `gemini`, `openrouter`, `lmstudio`, `ollama` |
| `AGENT_MODEL` | per-provider | Model override (each provider has a sensible default) |
| `OPENROUTER_API_KEY` | — | API key for OpenRouter |
| `ANTHROPIC_API_KEY` | — | API key for Anthropic |
| `OPENAI_API_KEY` | — | API key for OpenAI |
| `GOOGLE_API_KEY` | — | API key for Google Gemini |
| `idle_timeout` | `2.0` | Per-tool idle timeout in seconds (in tool config) |
| `max_turns` | `15` | Per-subtask turn budget (in `models.py`) |

Local providers (`lmstudio`, `ollama`) don't need API keys.

## Project structure

```
clive.py          — orchestrator: plan → execute → summarize
planner.py        — LLM decomposes task into subtask DAG (JSON)
executor.py       — DAG scheduler + per-subtask worker loops
session.py        — tmux session/pane management
toolsets.py       — tool registry with named profiles (minimal, standard, full, remote)
models.py         — dataclasses: Subtask, Plan, SubtaskResult, PaneInfo
llm.py            — multi-provider LLM client (OpenAI, Anthropic, Gemini, OpenRouter, LMStudio, Ollama)
prompts.py        — all LLM prompt templates
completion.py     — three-strategy completion detection (marker/prompt/idle)
tools/            — helper scripts
  youtube.sh      — YouTube: list/get/captions/transcribe
  podcast.sh      — Podcast: list/get/transcribe
  claude.sh       — Anthropic Messages API wrapper
fetch_emails.sh   — IMAP email fetcher (used by the email tool)
send_reply.sh     — email sender via msmtp
requirements.txt  — Python dependencies
TOOLS.md          — full tool catalog and profile documentation
.env              — API keys (not committed)
```
