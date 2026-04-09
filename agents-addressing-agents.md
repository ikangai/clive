# Agents Addressing Agents

Multi-agent communication is an active design space. Google shipped A2A as a dedicated agent-to-agent protocol. Anthropic has MCP for connecting agents to tools and data. Every serious agent framework has some kind of inter-agent wire format — JSON-RPC, function calls, structured messages with typed payloads.

The assumption behind all of them is that agents need a protocol to communicate. Something machine-readable. Something with a schema.

Clive agents already live in terminals. They read text, they type text. If one agent's terminal output is another agent's terminal input, you don't need a new protocol. You need SSH.

## `clive@host`

The addressing model is as simple as it looks:

```bash
clive "clive@devbox check disk usage and report"
```

The outer Clive parses `clive@devbox` from the task text, resolves the host (via an optional YAML registry or auto-resolve), opens an SSH connection in a new tmux pane, and routes the task. The inner Clive — the one running on devbox — receives the task, executes it in its own habitat with its own tools, and reports back.

From the outer agent's perspective, the agent pane is just another pane. It reads the screen, it sees text, it types when appropriate. The same loop that drives lynx or mutt or a shell drives a remote Clive instance. No special case in the architecture.

Chaining works naturally:

```bash
clive "clive@gpu render the video then clive@web upload it to S3"
```

Two addresses, two agent panes, sequenced by the planner. Each remote instance is autonomous — it has its own tools, its own model, its own judgment.

## The conversation protocol

One thing does change when an agent talks to another agent versus a CLI tool: turn-taking matters.

When driving `grep`, the agent types and immediately reads the result. When driving another Clive, the agent needs to know whether the remote is still working, has a question, or is done. Without this signal, the outer agent would either interrupt work in progress or waste tokens re-reading an unchanged screen.

The solution is a text protocol — but text in the most literal sense. The inner Clive prints lines to stdout:

```
TURN: thinking       ← I'm working, don't type
PROGRESS: step 2     ← status update
QUESTION: "which format?"  ← I need input
CONTEXT: {"result": "summary here", "files": ["report.pdf"]}
TURN: done           ← finished
```

The outer agent reads these lines the same way it reads any terminal output. `TURN: thinking` means wait. `TURN: waiting` means respond. `TURN: done` means extract the result from `CONTEXT` and move on. The executor skips the LLM call entirely during `thinking` turns — if the inner agent is working, there's nothing for the outer agent to decide.

This isn't a structured protocol in the way A2A or MCP are structured. It's text on a screen. The outer agent reads it with the same parser it uses for everything else. The inner agent writes it with `print()`. The "protocol" is a handful of keywords and a JSON blob.

## Bring your own LLM

Here's a detail that has larger implications than it might appear: the remote Clive instance uses your API keys, not its own.

When the SSH connection opens, the API key environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`) are forwarded via SSH `SendEnv` — which requires a matching `AcceptEnv` directive in the remote host's SSH daemon configuration. The remote Clive reads them from its environment and uses them for inference. No keys are stored on remote hosts. No central key management. No token brokering service.

This means the intelligence is decentralized. Each node in the topology brings its own brain — or more precisely, borrows yours for the duration of the session. When the SSH connection closes, the keys disappear. The remote host never had them on disk.

The implications for deployment are worth spelling out: any machine with Python, tmux, and SSH (configured to accept the forwarded environment variables) becomes a Clive node. No registration. No API keys to provision on remote hosts. The operator's keys travel with the session, and the remote host provides the environment — the installed tools, the filesystem, the network position.

## The cost curve

Agent-to-agent is the headline feature, but the work underneath it matters more for daily use.

The original Clive architecture invoked the LLM on every turn, for every task. This is correct but expensive. Most tasks don't need it.

The current routing works in three tiers:

**Tier 0: Regex.** If the task starts with `ls`, `curl`, `grep`, `jq`, or any of ~40 common commands, it routes directly to the shell. Zero LLM calls. Cost: nothing.

**Tier 1: Fast classifier.** A small, cheap model classifies intent — is this a question, a direct command, a script task, or something that needs a full plan? One LLM call, small model. Most tasks resolve here.

**Tier 2: Full planner.** Complex multi-step tasks get the full DAG planner. Multiple subtasks, parallel execution, dependency tracking. This is where the full model earns its keep.

Within execution, the same principle applies. Script-mode subtasks generate a shell script in one LLM call and execute it mechanically. Interactive mode only engages when the task genuinely requires observation — browsing, debugging, exploring unknown systems. The token difference is roughly 2.5x.

The turn-state protocol extends this to agent panes. When the inner Clive says `TURN: thinking`, the outer Clive doesn't call its LLM. It sleeps and re-reads the screen in two seconds. An agent conversation that involves thirty seconds of inner-agent work costs zero outer-agent tokens during that window.

The governing principle: LLM where judgment is required, shell everywhere else. Applied recursively across agents.

## The topology

What makes this interesting is where it goes.

The addressing model is flat. `clive@host` is an address. Any machine reachable via SSH is a valid host. The topology is whatever your SSH config allows:

```
your laptop
  └── tmux session "clive"
        ├── pane: shell          (local)
        ├── pane: agent-devbox   (ssh → devbox → clive)
        ├── pane: agent-gpu      (ssh → gpu-cluster → clive)
        └── pane: agent-prod     (ssh → bastion → prod → clive)
```

Each remote Clive has its own tools, its own network position, its own filesystem. The GPU box has CUDA. The prod server has access to the database. Your laptop has your browser. The outer Clive orchestrates, each inner Clive executes with what's locally available.

This is the multi-agent topology that falls out naturally when agents inhabit terminals and SSH is the connection between habitats. No service mesh. No agent registry service. No message broker. Just `clive@host` and the Unix tools that have been doing secure remote execution for thirty years.

The code is at [github.com/ikangai/clive](https://github.com/ikangai/clive).
