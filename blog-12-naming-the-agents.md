# Naming the Agents

Previous posts described clive instances as ephemeral — a process starts, does a task, exits. Even the server mode discussion treated instances as anonymous workers pulling jobs from a queue. The addressing scheme, `clive@host`, identified machines, not instances. You could reach the clive on devbox, but you couldn't reach a specific clive on devbox.

This changes when instances get names.

## The name is the contract

```bash
clive --name researcher "monitor arxiv for new papers on tool use"
```

This clive instance is called `researcher`. It starts, does the initial task, and then — unlike unnamed instances — stays alive. It writes a file to `~/.clive/instances/researcher.json` announcing its presence: PID, tmux session, toolset, start time. When it exits, the file is deleted.

The name implies a contract: if you have a name, you're listening. A named instance creates a dedicated `conversational` window in its tmux session — a pane that waits for input, executes tasks when they arrive, emits results, and waits again. Between tasks, the instance is idle but alive. Its shell has the working directory from the last task. Its pane agents have accumulated memory and shortcuts. Its environment carries forward.

Unnamed clive is a verb — run this task. Named clive is a noun — a persistent agent with an identity and a mailbox.

## Finding each other

The discovery mechanism is deliberately primitive. No daemon. No service. No coordination protocol. Just a directory.

```
~/.clive/instances/
  ├── researcher.json
  ├── gpu-worker.json
  └── mybot.json
```

Each file is a JSON document with everything needed to reach the instance: its name, PID, tmux session name, tmux socket, toolset, and whether it's conversational. Listing instances is `ls`. Checking liveness is `kill -0 $pid`. Pruning crashed instances is: if the PID is dead, delete the file.

This is the same strategy that PID files have used since the 1980s. It's fragile in theory — a crashed process leaves an orphaned file until something cleans it up. In practice, every read operation checks liveness first, so stale entries are invisible to consumers and get pruned on access. The failure mode is a leftover file on disk, not a service outage.

Name collisions are handled at startup. If `researcher.json` exists and the PID is alive, a second `clive --name researcher` fails immediately: "Instance 'researcher' is already running (PID 48201)." No race condition — the PID check is atomic enough for a single-user system, and for multi-user systems the file ownership provides the boundary.

## Local addressing

The registry enables something that wasn't possible before: addressing a clive instance on the same machine without SSH.

```bash
clive "clive@researcher summarize today's papers"
```

The address `clive@researcher` triggers the existing resolution chain, but with a new first step. Before checking the remote agent registry, before attempting SSH, the resolver looks in `~/.clive/instances/`. If it finds `researcher.json` with a living PID and `conversational: true`, it resolves locally.

Local resolution produces a tmux attach command instead of an SSH command:

```python
"tmux -L clive attach -t clive-a1b2c3d4:conversational"
```

The outer clive opens a pane in its own session, attaches to the inner clive's conversational window, types the task, and reads the TURN: protocol from the screen. From the executor's perspective, this pane is identical to a remote agent pane. Same protocol. Same parsing. Same timeout handling. The only difference is that the transport is tmux instead of SSH, and the latency is microseconds instead of milliseconds.

This is the key design choice: local addressing is not a separate mechanism. It's the fast path of remote addressing. The same code that handles `clive@devbox` over SSH handles `clive@researcher` via tmux. The inner clive doesn't know — or care — whether the task came from a local peer, a remote peer, or a human who attached to the session and typed it manually. The conversational pane is the universal inbox.

If the local name doesn't resolve — no registry entry, dead PID, not conversational — resolution falls through to SSH as before. A local instance can shadow a remote host with the same name. This is useful for testing: name your local instance `devbox` and all `clive@devbox` tasks stay on your machine.

## The dashboard

Once instances have names and a registry, visibility is trivial.

```
$ clive --dashboard

 CLIVE INSTANCES
 ───────────────────────────────────────────────────────
  NAME          PID     TOOLSET          STATUS    UPTIME
  researcher    48201   research+web     working   2h 14m
  gpu-worker    48305   standard+ai      idle      5h 41m
  mybot         48410   standard+media   idle      0h 03m

 TASKS IN PROGRESS
 ───────────────────────────────────────────────────────
  researcher    "summarize today's papers on tool use"
                step 2/4 · browser pane · 1,240 tokens

 3 instances · 1 busy · 0 queued
```

`clive --dashboard` reads the registry, prunes dead entries, peeks at each instance's tmux session for TURN: state, and prints a table. It exits after printing — same interaction model as `docker ps`. The TUI gets the same view as a `/dashboard` slash command with a two-second refresh.

The dashboard also shows remote instances from `~/.clive/agents.yaml`, tagged as `remote` with a connectivity indicator. One view for everything — local named instances and remote registered hosts, all addressable from the same input line.

This is not a monitoring system. There are no metrics, no alerts, no dashboards-as-a-service. It's `ps aux` for clive instances — a snapshot of what's running, what's busy, and how to reach it.

## The topology that names enable

Without names, the clive topology is hub-and-spoke. One instance orchestrates, others execute. The orchestrator knows about remote hosts, but remote hosts don't know about each other, and nothing on the local machine knows about anything else on the local machine.

With names, the topology becomes a peer mesh. Every named instance is discoverable. Any instance can address any other instance. The researcher can ask the gpu-worker to render a chart. The gpu-worker can ask mybot to upload it. No central orchestrator required — any node can initiate work on any other node.

```
researcher ──── clive@gpu-worker "render chart from data.csv"
                     │
                     └── clive@mybot "upload chart.png to S3"
```

The planner already handles multi-agent tasks. `clive@gpu-worker` in the task text creates an agent pane. The only thing that changed is that `gpu-worker` resolves locally instead of over SSH. The execution model, the TURN: protocol, the dependency tracking — all unchanged.

This is also where the naming convention starts to matter. `clive@researcher` is more useful than `clive@48201` or `clive@laptop-a1b2`. Names carry intent. A team might run `clive --name code-review`, `clive --name ci-monitor`, `clive --name deploy-bot` on a shared server. The names are the API. The dashboard is the service catalog.

## What names don't solve

Names don't provide authentication. Any process that can read `~/.clive/instances/` and access the tmux socket can attach to any named instance. On a single-user machine this is fine. On a shared server, tmux socket permissions and file ownership provide basic isolation, but there's no token-based auth or access control.

Names don't provide fault tolerance. If `researcher` crashes, it's gone — the registry file becomes stale and gets pruned. There's no supervisor that restarts it, no health check that pages someone. Named instances are long-lived but not durable. They're closer to screen sessions than to systemd services.

And names don't solve the coordination problem. Two instances can address each other, but they can't negotiate, split work, or resolve conflicts. If `researcher` and `gpu-worker` both try to write to the same file, they'll race. The shared brain from pane agents works within a single instance but not across instances — that's a separate problem that needs the cross-process IPC work.

What names do solve is the identity problem. Before names, clive instances were anonymous processes with PIDs. After names, they're addressable agents with persistent state and a way to reach them. That's the foundation everything else builds on — the dashboard, the local addressing, the peer topology. The fancy features are just consequences of giving each agent a way to say "I'm here, and my name is researcher."
