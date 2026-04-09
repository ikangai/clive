# When a CLI Becomes a Server

clive started as a command-line tool. You type a task, it spins up tmux panes, the agent does the work, you get a result. One user, one task, one process. The architecture assumed this. Session names are hardcoded. Tmux sessions are killed on startup. The process exits when the task is done.

Then we added `clive@host`. SSH into a remote machine, run a task there, get results back. Suddenly clive is the thing sitting on the other end of an SSH connection, waiting for work. And SSH connections come from the network. From multiple users. At the same time.

A CLI tool that accepts tasks over SSH is a server. It just doesn't know it yet.

## The accidental server

The gap between "CLI tool" and "server" is wider than it appears. Consider what happens when two users SSH into the same clive host simultaneously.

User A connects and runs a task. clive creates a tmux session named `clive`, sets up panes, starts working. User B connects thirty seconds later. clive creates a tmux session named `clive` — and the `kill_session=True` flag destroys User A's session. User A's half-finished task vanishes.

```python
session = server.new_session(
    session_name=session_name,
    kill_session=True,  # this is the problem
    attach=False,
)
```

This is correct behavior for a CLI tool. You don't want stale sessions from crashed runs blocking new ones. It's catastrophic behavior for a server.

The session name collision is just the most obvious issue. The process model is wrong too. Each clive invocation is a standalone Python process that owns its tmux session for the duration of the task and cleans up on exit. There's no persistence between requests. No queue. No coordination. If two tasks arrive simultaneously, they race on everything — tmux sessions, the `/tmp/clive/` directory, even the tmux server socket.

## What a server needs

Web servers solved this decades ago. The solution has three parts: a queue that accepts requests, a pool of workers that process them, and isolation between concurrent requests.

For clive, the mapping is direct. A task arrives over SSH. It goes into a queue — a directory of JSON files, one per job, ordered by timestamp. A worker process picks the oldest pending job, marks it running, creates an isolated tmux session with a unique name, executes the task, writes the result back to the job file. The SSH connection polls until the result appears.

```
ssh clive@host "summarize the quarterly report"
  → enqueue job → worker picks it up → clive.run() in isolated session → result
```

The queue needs exactly one property: atomic dequeue. Two workers polling simultaneously must not pick the same job. File locking handles this — `fcntl.flock` on a lockfile in the queue directory. The worker acquires the lock, scans for the oldest `pending` job, marks it `running`, releases the lock. Simple, no external dependencies, survives process crashes because abandoned jobs stay `pending`.

The worker pool is a supervisor that forks N processes. Each worker has its own tmux session, its own PID in the session name, its own `/tmp/clive/{session_id}/` directory. Workers self-terminate after a configurable number of jobs to prevent memory accumulation from long-running LLM sessions. The supervisor restarts them.

```bash
python clive.py --serve --workers 4 --queue-dir ~/.clive/queue
```

This is not novel architecture. It's a pre-fork worker model — the same pattern as Gunicorn, Unicorn, and PHP-FPM. The insight is that clive doesn't need a custom concurrency model. It needs the concurrency model that's been handling HTTP requests since the 1990s, adapted for tmux sessions instead of socket connections.

## Bring your own intelligence

The `clive@host` addressing model has an unusual property: the caller brings the LLM API key, not the server.

When the SSH connection opens, `SendEnv` forwards the caller's `ANTHROPIC_API_KEY` (or OpenAI, or OpenRouter). The remote clive instance reads it from the environment and uses it for inference. When the SSH session closes, the key is gone. Nothing is stored on disk.

This inverts the economics of running a service. The server operator provides compute, storage, installed tools, and network position. The user provides intelligence. A clive host with GPU tools, media processing software, and access to internal databases is valuable even with zero LLM budget — because every user brings their own.

This also means there's no central billing, no API key management, no token accounting service. The user's API provider bills them directly for the tokens their tasks consume. The server operator's cost is the machine itself.

The implication for multi-tenancy is that resource limits need to be about compute, not tokens. The server doesn't care how many tokens a user burns — that's between the user and their API provider. What the server cares about is CPU time, memory, disk space, and process count. These are the resources that are shared and exhaustible.

## The sandbox problem

A CLI tool running as your own user on your own machine doesn't need a sandbox. You trust yourself. A CLI tool running tasks from SSH connections needs hard boundaries.

The current safety mechanism is `BLOCKED_COMMANDS` — a list of regex patterns matching destructive commands like `rm -rf /`, `mkfs`, `shutdown`. The LLM generates a command, the pattern matcher checks it, blocked commands are rejected. This was designed for the single-user case, where the threat model is "the LLM had a bad idea." It does not hold against an adversarial user.

The gap: `BLOCKED_COMMANDS` only checks what the LLM generates. If a user's task prompt causes the LLM to pipe output through `bash`, or use `python -c`, or create a script that does the dangerous thing indirectly — the regex doesn't see it. The command `python3 -c "import shutil; shutil.rmtree('/')"` doesn't match any blocked pattern.

For a server, the sandbox has to be at the OS level, not the application level. The command runs inside a restricted environment where the filesystem is read-only except for the session directory, process count is capped, memory is bounded, and optionally the network is filtered. On Linux, bubblewrap provides this with namespace isolation. On macOS, `sandbox-exec` provides a weaker but workable equivalent. The minimum viable sandbox is `ulimit` — crude but available everywhere.

```bash
bwrap --ro-bind / / --bind "$WORKDIR" "$WORKDIR" \
      --tmpfs /tmp --unshare-pid --die-with-parent \
      /bin/bash -c "ulimit -u 64; ulimit -v 524288; $COMMAND"
```

The important detail: the sandbox wraps the execution, not the LLM. The LLM can generate any command it wants. The sandbox prevents that command from doing damage regardless of what it is. Defense in depth — the blocklist catches obvious mistakes cheaply, the sandbox catches everything else.

## The topology that emerges

When clive runs as a server, the addressing model scales without changes. A user's local clive orchestrates. Remote clive instances execute. Each remote instance is a server handling queued requests from multiple callers.

```
user A (laptop) ──ssh──→ clive@devbox (4 workers) ──ssh──→ clive@gpu
user B (laptop) ──ssh──→ clive@devbox              ──ssh──→ clive@prod
user C (laptop) ──ssh──→ clive@devbox
```

Three users share the devbox. Their tasks queue up and execute in order. The devbox itself can delegate to specialized hosts — a GPU cluster for rendering, a production bastion for database queries. The topology is whatever SSH allows, and the capacity at each node is however many workers the operator configures.

This is not a microservices architecture. There's no service mesh, no API gateway, no load balancer (though one could sit in front of SSH). It's closer to a university compute cluster: users SSH in, submit jobs, get results. The difference is that the jobs are natural language tasks executed by an LLM driving CLI tools, and the "job scheduler" is a directory of JSON files with a lockfile.

## What this doesn't solve

Server mode doesn't make clive a cloud platform. There's no authentication beyond SSH keys. There's no billing. There's no multi-region failover. There's no web dashboard showing queue depth and worker utilization (though a health file that the supervisor writes every five seconds gets you monitoring).

More fundamentally, the file-based queue won't scale past a few hundred jobs per minute. For most use cases — a team sharing a well-equipped devbox, a CI system delegating tasks, a personal fleet of clive nodes — this is fine. For a public service handling thousands of concurrent users, it isn't. The interface is abstract enough to swap in SQLite or Redis later, but the current implementation is intentionally simple.

The goal isn't to build a platform. It's to make the transition from "I run clive on my laptop" to "my team runs clive on a shared server" as small as possible. One flag: `--serve`. One config: the worker count. One requirement: SSH access. Everything else — the queue, the isolation, the worker lifecycle — follows from treating the CLI tool as what it becomes when you put it on a network: a server.
