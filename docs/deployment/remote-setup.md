# Clive as a Service: Remote Setup Guide

## Overview

"Clive as a service" turns a single Linux (or macOS) server into a shared
agent host.  Users SSH in, submit tasks, and get results back — the same
interface as running clive locally, but with multi-tenant isolation and
centralized resource management.

The security model follows **defense in depth**: every layer assumes the
layers above it can be bypassed.

```
┌──────────────────────────────────────────────────┐
│  SSH key auth only (no passwords, no forwarding) │
├──────────────────────────────────────────────────┤
│  ForceCommand → sandbox/run.sh wrapper           │
├──────────────────────────────────────────────────┤
│  Sandbox isolation (fs, process, memory, net)    │
├──────────────────────────────────────────────────┤
│  Per-user quotas (tokens, concurrency, disk)     │
├──────────────────────────────────────────────────┤
│  BLOCKED_COMMANDS regex layer in executor.py     │
└──────────────────────────────────────────────────┘
```

Even if a user crafts a prompt that tricks the LLM into generating a
dangerous command, the sandbox restricts filesystem writes to `/tmp/clive/$USER`,
process and memory limits prevent resource exhaustion, and the quota system
caps total LLM token spend.

---

## Prerequisites

| Requirement        | Version / Notes                                              |
|--------------------|--------------------------------------------------------------|
| Python             | 3.12+                                                        |
| tmux               | 3.2+ recommended (needed for the pane-based execution model) |
| Sandbox tool       | `bubblewrap` (Linux) or `sandbox-exec` (macOS, deprecated)   |
| SSH server         | OpenSSH 8.0+ with `/etc/ssh/sshd_config.d/` include support  |
| PyYAML             | For per-user quota files (`pip install pyyaml`)              |

On Debian/Ubuntu:

```bash
sudo apt install python3.12 tmux bubblewrap openssh-server
pip install pyyaml --break-system-packages
```

On macOS (development/testing only — `sandbox-exec` is deprecated):

```bash
brew install python@3.12 tmux
# bubblewrap is not available; macOS uses sandbox-exec as a fallback
```

---

## Step-by-step setup

### 1. Create the clive system user

```bash
# Linux
sudo useradd --system --create-home --shell /bin/bash clive
sudo mkdir -p /opt/clive /tmp/clive
sudo chown clive:clive /opt/clive /tmp/clive

# macOS (for testing)
sudo dscl . -create /Users/clive
sudo dscl . -create /Users/clive UserShell /bin/bash
sudo dscl . -create /Users/clive NFSHomeDirectory /var/empty
```

### 2. Install clive to /opt/clive

```bash
sudo -u clive git clone https://github.com/your-org/clive.git /opt/clive
# Or copy from a local checkout:
sudo rsync -a --exclude='.git' --exclude='__pycache__' . /opt/clive/
sudo chown -R clive:clive /opt/clive
```

Verify the sandbox wrapper is executable:

```bash
sudo chmod +x /opt/clive/sandbox/run.sh
```

### 3. Install the hardened sshd config

```bash
sudo cp /opt/clive/sandbox/sshd_clive.conf /etc/ssh/sshd_config.d/clive.conf

# Validate syntax before reloading
sudo sshd -t
# If no errors:
sudo systemctl reload sshd
```

This config applies a `Match User clive` block that:
- Forces all commands through `sandbox/run.sh`
- Disables all TCP/X11/agent/tunnel forwarding
- Restricts to key-based authentication only
- Accepts environment variables for API keys and config
- Limits to 3 concurrent sessions per user

### 4. Add authorized keys for users

Each user who should have access needs their public key added:

```bash
sudo mkdir -p ~clive/.ssh
sudo tee -a ~clive/.ssh/authorized_keys <<< "ssh-ed25519 AAAA... user@host"
sudo chown -R clive:clive ~clive/.ssh
sudo chmod 700 ~clive/.ssh
sudo chmod 600 ~clive/.ssh/authorized_keys
```

### 5. Configure API key forwarding (client side)

Users add to their `~/.ssh/config`:

```
Host clive-server
    HostName server.example.com
    User clive
    SendEnv ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY
    SendEnv LLM_PROVIDER AGENT_MODEL CLIVE_TOOLSET
```

The server's `AcceptEnv` directive in `sshd_clive.conf` allows these
specific variables through.  The API key never touches the server's disk —
it lives only in the process environment for the duration of the session.

### 6. Configure per-user quotas

Create a quota file at `/opt/clive/.clive/quotas.yaml`:

```yaml
# Default limits (apply to any user not listed below)
default:
  max_tokens_per_day: 100000
  max_concurrent: 3
  max_disk_mb: 1024
  max_wall_seconds: 3600

# Per-user overrides
alice:
  max_tokens_per_day: 500000
  max_concurrent: 5

bob:
  max_tokens_per_day: 50000
  max_wall_seconds: 1800
```

The quota system (`sandbox/quotas.py`) checks usage against these limits
before each task execution.  Fields not specified in a user override
inherit from the `UserQuota` defaults (100K tokens/day, 3 concurrent,
1024 MB disk, 3600s wall time).

### 7. Start the server

```bash
# Start in server mode with 4 workers
sudo -u clive python3 /opt/clive/clive.py --serve --workers 4 --queue-dir /opt/clive/.clive/queue

# Or run under systemd (recommended for production)
sudo cp /opt/clive/sandbox/clive.service /etc/systemd/system/  # if provided
sudo systemctl enable --now clive
```

Workers pull jobs from the queue directory, each in its own tmux session
and sandbox.  The supervisor automatically restarts any worker that exits
or crashes.

---

## Security layers in detail

### Layer 1: SSH key authentication

The `sshd_clive.conf` config disables password authentication entirely
for the clive user.  Only `publickey` is accepted.  This eliminates brute-
force attacks and credential stuffing.

All forwarding (TCP, X11, agent, tunnel) is disabled — users cannot use
the clive server as a proxy or pivot point.

### Layer 2: ForceCommand and the sandbox wrapper

Every SSH command is intercepted by `ForceCommand` and routed through
`/opt/clive/sandbox/run.sh`.  The user's original command is available as
`$SSH_ORIGINAL_COMMAND` but is always executed inside the sandbox.

Even if a user bypasses SSH and somehow gets a shell, the ForceCommand
directive ensures no direct command execution is possible through the
SSH channel.

### Layer 3: Sandbox isolation

`sandbox/run.sh` detects the platform and applies appropriate isolation:

- **Linux (bubblewrap):** Read-only root filesystem bind, writable only
  in `/tmp/clive/$USER`.  PID and UTS namespaces are unshared.  Home
  directory credentials (`.ssh`, `.aws`, `.config`) are hidden behind
  tmpfs mounts.  Memory limited to 512 MB (configurable via
  `CLIVE_SANDBOX_MEM_MB`), processes limited to 64.

- **macOS (sandbox-exec):** Seatbelt profile denies all writes except
  to the workdir and `/tmp`.  Network can be selectively denied with
  `--no-network`.  Note: `sandbox-exec` is deprecated by Apple but
  remains functional (see Troubleshooting).

- **Fallback:** `ulimit` only — minimal protection, not recommended for
  production.

### Layer 4: Per-user quotas

The quota system (`sandbox/quotas.py`) enforces four limits:

| Limit                 | Default  | Purpose                              |
|-----------------------|----------|--------------------------------------|
| `max_tokens_per_day`  | 100,000  | Cap LLM API spend per user           |
| `max_concurrent`      | 3        | Prevent one user from hogging workers |
| `max_disk_mb`         | 1,024 MB | Prevent disk exhaustion              |
| `max_wall_seconds`    | 3,600s   | Kill runaway tasks                   |

Quotas are checked before each task begins.  If any limit is exceeded,
the task is rejected with a descriptive error message.

### Layer 5: BLOCKED_COMMANDS regex

The executor (`executor.py`) checks every LLM-generated command against
a list of regex patterns before execution.  Blocked patterns include:

- `rm -rf /` and variants targeting home directories
- `shutdown`, `reboot`, `halt`, `poweroff`
- `mkfs`, `dd of=/dev/`, writes to `/dev/sd*`
- Fork bombs and infinite loops
- Base64-encoded eval payloads

**Important limitation:** This layer only inspects the literal command
string the LLM generates.  It does not catch indirect execution through
`python -c`, piped scripts, or other wrappers.  This is why the sandbox
layer (Layer 3) is essential — it provides the hard boundary that regex
cannot.

---

## ChrootDirectory trade-offs

The `sshd_clive.conf` includes a commented-out `ChrootDirectory` option:

```
# ChrootDirectory /opt/clive/jail
```

### When to use it

- You want an additional filesystem isolation boundary on top of the
  sandbox.
- You are running on a system without bubblewrap and want stronger
  isolation than `sandbox-exec` alone provides.

### What breaks

A chroot requires all dynamically linked binaries to have their libraries
available inside the jail.  For clive, this means:

```
/opt/clive/jail/
├── bin/           # bash, python3, tmux, coreutils
├── lib/           # libc, libpython, libncurses, etc.
├── lib64/         # (on 64-bit systems)
├── usr/
│   ├── bin/       # Additional tools from the toolset
│   └── lib/
├── tmp/           # Writable workspace
├── dev/
│   ├── null
│   ├── urandom
│   └── zero
└── etc/
    ├── passwd     # Minimal, just the clive user
    └── resolv.conf
```

Building this jail is non-trivial.  You need to trace every binary's
library dependencies (`ldd /usr/bin/python3`) and copy them in.  Any
toolset command that is not present in the jail will fail silently.

**Recommendation:** On Linux, prefer bubblewrap over chroot.  Bubblewrap's
`--ro-bind / /` provides equivalent filesystem restrictions without
needing to build a separate directory tree.  Use chroot only if bubblewrap
is unavailable and you need stronger isolation than the sandbox profile
alone.

---

## Server mode

### How `--serve` works

```bash
python clive.py --serve --workers 4 --queue-dir ~/.clive/queue
```

The `--serve` flag starts clive in **server mode**, which consists of:

1. **Supervisor** (`server/supervisor.py`): The main process that manages
   the worker pool.  It spawns N worker processes and monitors them in a
   loop.  If a worker exits (crash, max-jobs reached, or OOM kill), the
   supervisor spawns a replacement.

2. **Workers**: Each worker is a separate process with its own tmux
   session and working directory under `/tmp/clive/{session_id}/`.
   Workers pull jobs from the queue directory, execute them through the
   standard plan-execute-summarize pipeline, and write results back.
   Workers self-terminate after a configurable number of jobs
   (`worker_max_jobs`, default 50) to prevent memory accumulation from
   long-running LLM sessions.

3. **Job queue** (`server/queue.py`): A filesystem-based queue.  Jobs are
   JSON files in the queue directory.  Workers atomically claim jobs by
   renaming them.  No external dependencies (no Redis, no RabbitMQ).

### Graceful shutdown

Sending `SIGTERM` to the supervisor triggers a graceful shutdown:
- The supervisor stops accepting new jobs
- Running workers are terminated
- Workers are joined with a 10-second timeout

---

## Monitoring

### health.json

When started with a health path, the supervisor writes a health file
every 5 seconds (configurable via `health_interval`):

```json
{
  "status": "healthy",
  "workers": 4,
  "workers_alive": 4,
  "total_workers_started": 7,
  "uptime_seconds": 3621
}
```

The `total_workers_started` counter exceeding `workers` indicates
worker restarts have occurred.

Monitor with any standard tool:

```bash
# Simple health check
cat /opt/clive/.clive/health.json | jq .status

# Nagios/Icinga style
test "$(jq -r .status /opt/clive/.clive/health.json)" = "healthy" && echo OK || echo CRITICAL

# Watch for worker churn
watch -n5 'jq . /opt/clive/.clive/health.json'
```

### Logs

Clive uses Python's standard `logging` module.  In server mode, direct
output to a file:

```bash
python clive.py --serve --workers 4 2>&1 | tee /var/log/clive/server.log
```

Or configure systemd to capture journal output:

```ini
[Service]
StandardOutput=journal
StandardError=journal
SyslogIdentifier=clive
```

### Audit trail

Every task execution writes an audit record to `.clive/audit/`.  These
JSON files contain the task prompt, commands executed, token usage, and
outcome.  Use them for billing reconciliation and incident investigation.

---

## Troubleshooting

### SendEnv variables not arriving

**Symptom:** API calls fail with authentication errors even though keys
are set locally.

**Cause:** The SSH server must explicitly accept forwarded environment
variables.  Check both sides:

```bash
# Client: ensure SendEnv is in your SSH config
grep SendEnv ~/.ssh/config

# Server: ensure AcceptEnv is in the sshd config
sudo grep AcceptEnv /etc/ssh/sshd_config.d/clive.conf

# Server: verify sshd loaded the config
sudo sshd -T | grep acceptenv
```

If `sshd -T` does not list the expected variables, the config file may
not be included.  Check that `/etc/ssh/sshd_config` contains:

```
Include /etc/ssh/sshd_config.d/*.conf
```

### sandbox-exec deprecated on macOS

**Symptom:** Warning messages about `sandbox-exec` being deprecated.

**Status:** Apple deprecated `sandbox-exec` but has not removed it.  As
of macOS 15 (Sequoia), it still functions.  However:

- Apple may remove it without notice in a future release
- The Seatbelt profile language is undocumented and may change
- Some operations that work on Linux/bubblewrap may fail silently

**Recommendation:** Use macOS for development and testing only.  Deploy
production clive-as-a-service on Linux with bubblewrap.

### ForceCommand prevents interactive shells

**Symptom:** `ssh clive-server` drops the connection immediately or
returns an error.

**Expected behavior:** The `ForceCommand` directive routes everything
through `sandbox/run.sh`.  Without `$SSH_ORIGINAL_COMMAND`, the wrapper
receives an empty command.

**Fix:** Always pass a command when connecting:

```bash
ssh clive-server "python3 /opt/clive/clive.py 'summarize the news'"
```

Or, for interactive use, explicitly request a task:

```bash
ssh clive-server "python3 /opt/clive/clive.py --conversational"
```

### Workers crashing in a loop

**Symptom:** `total_workers_started` in health.json grows rapidly.

**Diagnosis:**

```bash
# Check supervisor logs
journalctl -u clive --since "5 minutes ago" | grep -i "worker.*exit"

# Check for OOM kills
dmesg | grep -i oom | tail -5

# Check disk space (workers fail if /tmp is full)
df -h /tmp
```

**Common causes:**
- Out of memory: increase `CLIVE_SANDBOX_MEM_MB` or reduce `--workers`
- Disk full: clean `/tmp/clive/` or increase `max_disk_mb` quota
- Missing dependencies: ensure all toolset commands are installed

### Permission denied on /tmp/clive

**Symptom:** Tasks fail with permission errors writing to the work
directory.

**Fix:**

```bash
sudo mkdir -p /tmp/clive
sudo chown clive:clive /tmp/clive
sudo chmod 1777 /tmp/clive  # sticky bit, like /tmp itself
```

### tmux session conflicts

**Symptom:** "duplicate session" errors or tasks interfering with each
other.

**Cause:** Each worker needs a unique tmux session.  If workers share a
session name, commands collide.

**Fix:** Ensure `/tmp/clive/` per-session directories are unique.  The
supervisor assigns each worker a process-specific working directory.
If sessions persist after a crash, clean them manually:

```bash
tmux kill-server  # nuclear option — kills all tmux sessions
# Or selectively:
tmux list-sessions | grep clive | cut -d: -f1 | xargs -I{} tmux kill-session -t {}
```
