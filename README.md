```
 ██████╗██╗     ██╗██╗   ██╗███████╗
██╔════╝██║     ██║██║   ██║██╔════╝
██║     ██║     ██║██║   ██║█████╗
██║     ██║     ██║╚██╗ ██╔╝██╔══╝
╚██████╗███████╗██║ ╚████╔╝ ███████╗
 ╚═════╝╚══════╝╚═╝  ╚═══╝  ╚══════╝
```

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

## Install

**One-liner:**
```bash
curl -sSL https://raw.githubusercontent.com/ikangai/clive/main/install.sh | bash
```

**Or manually:**
```bash
git clone https://github.com/ikangai/clive.git
cd clive
bash install.sh
```

The installer checks prerequisites (Python 3.10+, tmux), creates a venv, installs Python deps, offers to install CLI tools for your chosen profile, configures your LLM provider, and creates `clive` / `clive-tui` launcher commands.

**Supported platforms:** macOS (brew), Ubuntu/Debian (apt), Fedora/RHEL (dnf), Arch (pacman). **Windows:** clive requires tmux, which is not available natively — use [WSL](https://learn.microsoft.com/en-us/windows/wsl/install) and run the installer inside your WSL terminal.

**Quick start after install:**
```bash
clive "list files in /tmp and summarize"          # CLI mode
clive -t standard "browse example.com"            # with browser + data tools
clive-tui                                          # TUI mode
clive --list-tools                                 # see what's available
```

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

1. **Plan** — The LLM decomposes your task into subtasks with dependencies, forming a DAG. Each subtask is assigned an **observation level** — how closely the agent watches the terminal during execution.
2. **Execute** — Independent subtasks run in parallel on different tmux panes; dependent subtasks wait for their prerequisites. Each subtask runs in an isolated session directory (`/tmp/clive/{session_id}/`).
3. **Summarize** — Results from all subtasks are synthesized into a final report

### Execution modes

The planner assigns an execution mode per subtask — how much the agent observes during execution:

| Mode | How it works | When to use | LLM calls |
|---|---|---|---|
| **direct** | Execute a literal shell command. No LLM involved. | Simple commands the classifier recognizes directly. | 0 |
| **script** | Generate a shell script → execute in one shot → check exit code. On failure, read error and repair. | Deterministic single-step pipelines, file ops, data extraction, known API calls. | 1 (+ repairs) |
| **planned** | Generate a sequence of commands with verification criteria → execute each mechanically → check exit code per step. No LLM calls during execution. | Deterministic multi-step workflows: install+configure, fetch+process+save, multi-file operations. | 1 |
| **llm** | The model *is* the tool. Read input files from the session dir (plus any absolute paths in the task), make one LLM call, write the result to `llm_<id>.txt`. No pane, no shell. | Translation, summarization, rewriting, extraction, classification, explaining, answering from provided content — tasks where generation *is* the work. | 1 |
| **interactive** | Read screen → reason → type command → repeat. Full turn-by-turn loop with observation classification. | Multi-step exploration, debugging, unknown content, interactive applications. | N turns |
| **streaming** | Like interactive, with automatic intervention detection for prompts, passwords, and confirmations. | Package installs, operations requiring passwords, long-running processes. | N turns |

The planner defaults to `script` or `planned` when the task is deterministic, and to `llm` when the task is a text transformation that shell can't do. `interactive` engages when the task requires observation and adaptation. Chains are first-class: a task like "fetch the transcript and translate it" produces a `script` → `llm` DAG where the transcript file flows between subtasks through the session working directory.

### Observation loop efficiency

The interactive and streaming modes use a three-phase observation architecture that minimizes LLM costs:

```
WAIT (free)          OBSERVE (cheap)        DECIDE (expensive)
markers, polling     regex classifier       main model
exit codes           event formatting       only when needed
intervention detect  compact summaries
```

**Per-pane model selection** — Each pane declares its own model tier via driver frontmatter. Shell and data panes use fast/cheap models (Haiku, Flash); browser and email use the default model. The tier system resolves labels like `fast` to concrete model names based on the active provider.

**Observation classifier** — After each command, a regex-based `ScreenClassifier` categorizes the screen state (success/error/needs_input/running) and decides whether the main model needs to be consulted. On success, a compact event like `[OK exit:0] file1.txt\nfile2.txt` replaces the full screen diff — cutting token usage by 60-80%.

**Progressive context compression** — Instead of dropping old conversation turns (the bookend trim), a cheap model summarizes them into a running history. The main model sees: system prompt + compressed history + current screen.

**Native tool calling** — When the provider supports it (OpenAI, Anthropic, Gemini, OpenRouter), the interactive runner uses native tool calls (`run_command`, `read_screen`, `complete`) instead of text-based command extraction. This enables command batching — multiple commands per LLM response — reducing turn count.

**Streaming observation (v0.7.0)** — In addition to the post-command classifier, each pane now also streams raw bytes through `tmux pipe-pane` into a per-pane FIFO, where an async byte classifier detects ANSI SGR alerts (red/yellow text, blink), known prompts (`password:`, `[y/N]`), error keywords (`Traceback`, `FATAL`), and command-end markers in real time. The runner's `wait_for_ready` blocks on these events instead of polling `capture-pane` — so the agent sees a colored error the moment the bytes arrive, not up to 500 ms later. ANSI-only signals that `capture-pane -p` silently strips (status bars, color-without-text changes) are no longer invisible.

Default-on; `CLIVE_STREAMING_OBS=0` opts out to the polling path. An opt-in speculation scheduler (`CLIVE_SPECULATE=1`) can fire the main LLM call speculatively on high-confidence events so inference overlaps with pane settling; version-stamped cancel-on-supersede guarantees ordering, and bounded concurrency + a circuit breaker cap the cost.

### Session state across tasks

The REPL and TUI hold state across tasks so follow-ups work naturally:

- **Persistent working directory** — Each REPL/TUI session has one `/tmp/clive/<id>/` directory that lives for the whole session. Files produced by an earlier task stay there; a follow-up like "translate the transcript into german" resolves because `transcript.txt` is still on disk.
- **File listing in the prompt** — The classifier and planner both see a compact listing of user-created files in the session dir when deciding how to route the next task, so references to "the transcript" don't require clarification.
- **Recent-task history** — The last several `(task, summary, produced_files)` tuples are rendered into the classifier and planner prompts. Useful when the follow-up references not a file but a prior subject ("now do the same thing for the other channel").

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

## TUI mode

clive includes an interactive terminal UI built with [Textual](https://textual.textualize.io/). Launch with `clive-tui` or `clive --tui`.

The TUI provides a single-screen interface: scrolling output on top, input line at the bottom. Type a task to execute it, or use slash commands for configuration.

**Slash commands:**

| Command | Description |
|---|---|
| `/profile <name\|+cat>` | Switch toolset profile or add a category |
| `/provider <name>` | Switch LLM provider |
| `/model <name>` | Switch model |
| `/tools` | Show available and missing tools |
| `/install` | Install missing CLI tools |
| `/status` | Show running task status |
| `/cancel` | Cancel the running task |
| `/clear` | Clear the screen |
| `/dashboard` | Show running clive instances |
| `/selfmod <goal>` | Self-modify clive (experimental) |
| `/undo` | Roll back last self-modification |
| `/safe-mode` | Disable self-modification for this session |
| `/help` | Show help |

**Task intelligence:** Before executing, clive triages your input through an LLM call that classifies it as a question about clive (answered directly), a task requiring clarification (asks you a follow-up question), or a task ready to execute. This avoids failed tasks from ambiguous instructions.

**Concurrent tasks:** Multiple tasks can run in parallel. Use `/status` to see active tasks and `/cancel` to stop them.

## Self-modification (experimental)

clive can modify its own source code through a governed pipeline with separation of powers. This is an experimental feature — enable it by setting `CLIVE_EXPERIMENTAL_SELFMOD=1` in your `.env` file.

### How it works

```
User goal
    │
    ▼
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ Proposer │────►│ Reviewer │────►│ Auditor  │────►│   Gate   │
│  (LLM)   │     │  (LLM)   │     │  (LLM)   │     │ (regex)  │
│          │     │          │     │          │     │          │
│ Generates│     │ Checks   │     │ Checks   │     │ Scans    │
│ code     │     │ quality  │     │ governance│    │ patterns │
└──────────┘     └──────────┘     └──────────┘     └─────┬────┘
                                                         │
                                                    ┌────▼────┐
                                                    │ Snapshot│
                                                    │  (git)  │
                                                    └────┬────┘
                                                         │
                                                    ┌────▼────┐
                                                    │  Apply  │
                                                    └─────────┘
```

Three independent LLM roles check each other:

1. **Proposer** — generates the code change based on the user's goal
2. **Reviewer** — independently evaluates code quality, correctness, and safety
3. **Auditor** — independently verifies governance compliance and tier rules

A **deterministic gate** (regex-based, no LLM) has final veto power. It scans all proposed changes for banned patterns and enforces file tier rules. Because it runs deterministic pattern matching, it cannot be "talked past" by any LLM.

### File tiers

Files are classified into tiers by risk level. Higher tiers require more approvals:

| Tier | Required approvals | Files |
|---|---|---|
| **IMMUTABLE** | Cannot be modified | `selfmod/gate.py`, `.clive/constitution.md`, `.clive/audit/*` |
| **GOVERNANCE** | Proposer + Reviewer + Auditor (unanimous) | `selfmod/*.py` (except gate.py), `.env` |
| **CORE** | Proposer + Reviewer | `clive.py`, `llm.py`, `executor.py`, `planner.py`, `session.py`, `models.py`, `prompts.py` |
| **STANDARD** | Proposer (Reviewer advisory) | `tui.py`, `toolsets.py`, `completion.py`, `install.sh` |
| **OPEN** | Proposer only | `tools/*`, temp files, everything else |

### Safety mechanisms

- **Deterministic gate** — regex-based pattern scanner with unconditional veto. Rejects `eval()`, `exec()`, `os.system()`, `shell=True`, obfuscated code, and more.
- **Immutable anchor** — `gate.py` and `constitution.md` can never be modified by the selfmod system.
- **Append-only audit trail** — every attempt (approved or rejected) is logged to `.clive/audit/` with hash-chained integrity verification.
- **Git snapshots** — a tagged snapshot is created before every modification. Roll back with `/undo` or `--undo`.
- **Rate limit** — maximum 5 modifications per session.
- **Constitution** — governance rules defined in `.clive/constitution.md`, enforced by the gate.
- **Experimental flag** — disabled by default. Requires explicit opt-in via `CLIVE_EXPERIMENTAL_SELFMOD=1`.

### Usage

```bash
# CLI
clive --selfmod "add a /history command that shows past tasks"
clive --undo                    # roll back last modification
clive --safe-mode               # disable selfmod for this run

# TUI
/selfmod add a /history command that shows past tasks
/undo
/safe-mode
```

### Constitution

The constitution at `.clive/constitution.md` defines the governance rules. It specifies file tiers, required approvals, and banned patterns. The constitution itself is IMMUTABLE — it cannot be modified by the selfmod system.

See [.clive/constitution.md](.clive/constitution.md) for the full document.

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

# Show all tools across all surfaces
python clive.py --list-tools

# Launch the TUI
python clive.py --tui

# Quiet mode — telemetry to stderr, only result to stdout
python clive.py --quiet "your task"
result=$(python clive.py -q "count files in /tmp")  # use as shell primitive

# Named instances — addressable, stay alive between tasks
python clive.py --name mybot "monitor server logs"
python clive.py --name mybot                       # no initial task, just listen
python clive.py --stop mybot                       # send SIGTERM to named instance
python clive.py --dashboard                        # show running instances

# Agent-to-agent — delegate to remote or local Clive instances
python clive.py "clive@devbox check disk usage"
python clive.py "clive@mybot summarize the logs"   # routes to local named instance
python clive.py "clive@gpu render video then clive@web upload it"

# Conversational mode — for clive-to-clive peer dialogue (auto-detected)
python clive.py --conversational "your task"

# Self-modification (experimental)
python clive.py --selfmod "your modification goal"
python clive.py --undo
python clive.py --safe-mode "your task"

# Run evals
python evals/harness/run_eval.py --layer 2              # all Layer 2 evals
python evals/harness/run_eval.py --layer 2 --tool shell  # shell evals only

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
| `remote` | shell, email, browser (remote), files (remote) | Remote server work |

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

**Note:** `clive@host` agent addressing handles this automatically — the generated SSH command enables `ControlMaster=auto` with sockets under `~/.clive/ssh/` (see `agents.build_agent_ssh_cmd`). The block above is for custom SSH panes you declare by hand in a toolset.

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

### Agent-to-agent (`clive@host`)

The most interesting topology: one agent driving another. Use `clive@host` addressing — no pane config needed:

```bash
# Ask a remote Clive to do something
clive "clive@devbox check disk usage and report"

# Chain multiple agents
clive "clive@gpu render the video then clive@web upload it to S3"

# Loopback (local agent-to-agent)
clive "clive@localhost read HN and summarize"
```

Clive auto-resolves the address via SSH, creates an agent pane on-demand, and routes the task. The remote Clive runs in conversational mode, emitting framed turn-state messages the outer Clive decodes into human-readable pseudo-lines (`⎇ CLIVE» turn=thinking`, `⎇ CLIVE» question: "..."`) that the driver prompt keys on. Raw protocol bytes never reach either side's LLM — the frame grammar is authenticated with a per-session nonce, so a compromised remote LLM cannot forge state, request spurious inference, or spoof a completion.

**BYOLLM** — two modes, automatic:

- **Cloud providers** (Anthropic, OpenAI, OpenRouter, Gemini): your API keys are forwarded via SSH `SendEnv`, the remote calls the cloud endpoint directly. No keys stored on remote hosts.
- **Local providers** (LMStudio, Ollama): the remote cannot reach your laptop's localhost, so clive transparently switches the remote to `LLM_PROVIDER=delegate`. Every inference round-trips back over the SSH channel to your laptop's LMStudio/Ollama via a framed `llm_request`/`llm_response` exchange. No tunneling, no `ssh -R`, no network changes on the remote.

Full docs: [`docs/byollm-delegate.md`](docs/byollm-delegate.md) covers the delegation protocol, the threat model, per-variable forwarding, self-hosted proxy support via `LLM_BASE_URL`, and a manual smoke-test procedure.

**`clive --agents-doctor`** — validate every host in `~/.clive/agents.yaml` in one command. Checks SSH reachability, remote clive install, AcceptEnv configuration, key file existence. Exits 0/1 so it composes into CI pipelines.

**Agent registry** (optional): Create `~/.clive/agents.yaml` to customize hosts, SSH keys, toolsets:

```yaml
devbox:
  host: devbox.local
  toolset: web
  key: ~/.ssh/agent_key
gpu:
  host: gpu.internal
  path: /opt/clive/clive.py
```

### Named instances & dashboard

Named instances are long-running clive processes that stay alive between tasks and are addressable via `clive@name`:

```bash
# Start a named instance
clive --name researcher "analyze competitor pricing"
# It runs the task, then waits for more work on stdin

# Address it from another clive
clive "clive@researcher now compare with our pricing"

# See all running instances
clive --dashboard
#  CLIVE INSTANCES
#  ───────────────────────────────────────────────────────
#   NAME          PID     TOOLSET          STATUS    UPTIME
#   researcher    48305   standard         idle      0h 14m

# Stop it
clive --stop researcher
```

**Core invariant:** If you have a name, you're conversational. A named instance stays alive after its initial task, listening for more work. This is the contract that makes local addressing work.

**Local-first resolution:** When `clive@researcher` is encountered, the local instance registry (`~/.clive/instances/`) is checked first. If a live, conversational instance matches, it resolves locally (microsecond latency via tmux attach) instead of SSH. Local instances can shadow remote hosts with the same name.

**Dashboard in TUI:** Use `/dashboard` in the TUI for the same view.

### Rooms — persistent multi-party chat (experimental)

Agent-to-agent (`clive@host`) is point-to-point. For round-table discussions involving three or more members — a council convening to review a design, a pool of specialists picking up whichever question matches their domain, a long-running channel where anyone can drop in — clive provides **rooms**: persistent Slack-channel-like venues brokered by an always-on lobby process. Threads inside a room enforce a first-class **pass-is-the-norm** round-robin so N clives don't talk over each other.

```bash
# Terminal A: start the lobby
python clive.py --role broker --name lobby

# Terminal B: alice auto-joins `general` on the lobby
python clive.py --name alice --conversational --join general@lobby

# Terminal C: bob
python clive.py --name bob --conversational --join general@lobby
```

On each `your_turn` grant the member's `drivers/room.md`-guided LLM responds with exactly one of `say: <body>` / `pass:`. The lobby fans messages to room observers, rotates the cursor to the next member, and enforces turn discipline so out-of-turn `say` frames are nacked.

**Design references:** the full 13-section design doc lives at [`docs/plans/2026-04-14-clive-rooms-design.md`](docs/plans/2026-04-14-clive-rooms-design.md) — covers room/thread model, turn discipline, breakout councils via private threads, the framed nonce-authenticated protocol extensions, the lobby state machine, persistence plan (Phase 5), and the full threat model.

**Status:** phases 0–4 shipped (protocol kinds, pure state machine, selectors-based IO server, client-side room runner, `--join` CLI flag with localhost resolution). Remote (SSH) transport and persistence / dropouts / summarization / rate limits come next. Enable with `python clive.py --role broker --name <lobbyname>` + `--join room@lobbyname` on each member.

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
| `SCRIPT_MODEL` | `AGENT_MODEL` | Model for script/planned mode generation (can be cheaper) |
| `CLASSIFIER_MODEL` | `gemini-3-flash` | Model for fast classification, context compression |
| `OPENROUTER_API_KEY` | — | API key for OpenRouter |
| `ANTHROPIC_API_KEY` | — | API key for Anthropic |
| `OPENAI_API_KEY` | — | API key for OpenAI |
| `GOOGLE_API_KEY` | — | API key for Google Gemini |
| `CLIVE_EXPERIMENTAL_SELFMOD` | `0` | Set to `1` to enable self-modification |
| `idle_timeout` | `2.0` | Per-tool idle timeout in seconds (in tool config) |
| `max_turns` | `15` | Per-subtask turn budget (in `models.py`) |
| `--quiet` / `-q` | off | CLI flag: telemetry to stderr, only result to stdout |

Local providers (`lmstudio`, `ollama`) don't need API keys.

## Project structure

```
clive.py              — entry-point wrapper (forwards to src/clive/)
tui.py                — TUI entry-point wrapper
install.sh            — cross-platform installer
requirements.txt      — Python dependencies
TOOLS.md              — full tool catalog and profile documentation

src/clive/            — main source package
  clive.py            — orchestrator: plan → execute → summarize
  models.py           — dataclasses: Subtask, Plan, SubtaskResult, PaneInfo
  config.py           — per-tool configuration (credentials, native config generation)
  output.py           — output routing: telemetry to stderr, results to stdout
  router.py           — 3-tier intent classification (direct → classifier → planner)

  llm/                — LLM inference layer
    llm.py            — multi-provider client with tool-calling support
    prompts.py        — prompt templates (planner, worker, script, planned, summarizer)
    tool_defs.py      — native tool definitions (run_command, read_screen, complete)
    delegate_client.py — stdio-based client for LLM_PROVIDER=delegate

  planning/           — task decomposition
    planner.py        — LLM decomposes task into subtask DAG (JSON)
    dag_scheduler.py  — parallel DAG execution with dependency tracking
    summarizer.py     — synthesizes results from all subtasks

  execution/          — mode-specific runners
    executor.py       — mode dispatcher + direct-mode worker
    runtime.py        — shared primitives: safety checks, sandbox, model tiers
    script_runner.py  — script mode: generate → execute → verify → repair
    interactive_runner.py — interactive mode: read-think-type loop
    planned_runner.py — planned mode: 1 LLM call → mechanical execution
    toolcall_runner.py — tool-calling mode: native tool calls, command batching
    skill_runner.py   — executable skill runner (zero LLM)

  observation/        — screen classification & context
    observation.py    — ScreenClassifier, ScreenEvent, format_event_for_llm
    completion.py     — completion detection (marker/prompt/idle) + intervention
    screen_diff.py    — screen diffing (60-80% token savings)
    context_compress.py — progressive context compression via cheap model
    command_extract.py — plain-text command extraction from LLM replies

  session/            — pane and tool management
    session.py        — tmux session/pane management, per-pane model resolution
    toolsets.py       — tool registry with named profiles
    commands.py       — CLI tool availability checking

  networking/         — agent-to-agent communication
    agents.py         — clive@host addressing, SSH command building
    protocol.py       — framed sentinel protocol (nonce-authenticated)
    registry.py       — instance registry (~/.clive/instances/)
    remote.py         — remote agent protocol, SCP file transfer
    dashboard.py      — running instances dashboard

  tui/                — terminal UI
    tui.py            — Textual-based TUI with slash commands
    tui_commands.py   — slash command handlers
    tui_task_runner.py — async task execution for TUI

  evolution/          — prompt evolution (experimental)
    evolve.py         — evolution loop
    evolve_fitness.py — fitness scoring via evals
    evolve_mutate.py  — LLM-driven prompt mutation

  selfmod/            — self-modification system (experimental)
  server/             — production server components
  sandbox/            — sandboxing (bwrap/sandbox-exec)
  drivers/            — auto-discovered driver prompts (per app_type)
  tools/              — helper scripts (youtube.sh, podcast.sh, etc.)

tests/                — 1039 unit tests
evals/                — eval framework (harness, layer1-4, baselines)
docs/                 — documentation, specs, plans
.clive/               — governance and audit data
.env                  — API keys and configuration (not committed)
```
