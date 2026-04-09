# Phase 3: Production Hardening — Sandboxing, Multi-Instance, Self-Modification

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make clive safe to expose over SSH to remote users, able to handle concurrent requests, and capable of safely modifying itself — the three pillars required to run clive "as a service."

**Architecture:** Five workstreams, ordered by dependency: (A) Remote sandbox boundaries, (B) Multi-instance server mode with request queuing, (C) Safe self-modification harness, (D) Local parallel coordination, (E) Agent-to-agent protocol completion. Each workstream is self-contained with its own tests.

**Tech Stack:** Python 3.12, tmux/libtmux, bubblewrap (bwrap) or Docker for sandboxing, Unix domain sockets + file-based locks for IPC, existing selfmod/ pipeline, existing agents.py/remote.py.

---

## Gap Analysis

### What exists today

| Area | Status | What works |
|------|--------|------------|
| Remote SSH | Implemented | `agents.py` address parsing, SSH cmd builder, `SendEnv` API key forwarding, `ensure_agent_pane()` |
| Execution safety | Partial | `BLOCKED_COMMANDS` regex list in executor.py, token/turn budgets, per-pane mutex locks |
| Self-modification | Partial | `selfmod/gate.py` (immutable scanner), `constitution.py` (5-tier system), `pipeline.py` (propose-review-audit-gate-apply) |
| Agent communication | Partial | `TURN:`/`DONE:`/`PROGRESS:`/`FILE:` protocol in `remote.py`, lazy pane creation |
| Multi-instance | Not started | Single-process, single-session, dies after task completion |
| Parallel local | Not started | No coordination between independent clive processes |

### Critical gaps (ordered by severity)

**1. Remote user sandbox (CRITICAL)**
- No filesystem boundary: remote user can `rm -rf ~`, read `/etc/shadow`, exfiltrate data
- No process isolation: can fork-bomb, mine crypto, open backdoor ports
- No network restriction: can lateral-move to internal services
- No resource caps: can consume all RAM/CPU/disk
- `BLOCKED_COMMANDS` is a regex allowlist on the LLM's generated commands only — a crafty prompt or piped command trivially bypasses it
- Session dir `/tmp/clive/{id}` is world-readable — cross-session data leakage possible
- SSH `SendEnv` trusts the remote `sshd_config` to accept it — misconfigured servers silently drop env vars

**2. Multi-instance / server mode (CRITICAL for "clive as a service")**
- Each `clive.py` invocation creates its own tmux session, runs task, exits — no persistence
- `session.py:34` does `kill_session=True` — a second invocation kills the first
- No request queue: SSH connections that arrive during execution are rejected or create races
- No load balancing: no way to distribute across workers
- No job persistence: if the process dies, the task is lost
- No rate limiting per-user (only per-session selfmod limit of 5)

**3. Self-modification under load (HIGH)**
- `pipeline.py` applies changes to the running codebase via `apply_changes()` — no restart mechanism
- No hot-reload: modified `executor.py` won't take effect until next invocation
- If clive is running as a server, self-modification while handling requests = undefined behavior
- `MAX_MODIFICATIONS_PER_SESSION = 5` is per-process, not per-user or per-time-window
- No rollback-on-failure after `apply_changes()` (snapshot exists but auto-rollback doesn't)
- Proposer/reviewer/auditor are all the same LLM instance — no real separation of concerns

**4. Local parallel coordination (MEDIUM)**
- Two local clive instances sharing tools (e.g., both want the `shell` pane) will race
- No discovery: clive A doesn't know clive B exists
- SharedBrain (in `pane_agent.py`) is per-process, not cross-process
- Session names collide unless `session_dir` differs (the suffix logic in `session.py:33` helps but isn't guaranteed unique for manual invocations)

**5. Agent protocol completion (MEDIUM)**
- `TURN: waiting` + `QUESTION:` flow isn't wired into the executor's interactive loop
- No automatic `FILE:` transfer after `TURN: done` (SCP infrastructure exists but isn't integrated into the worker loop)
- No timeout/retry for remote agent stalls
- No authentication beyond SSH keys — any SSH user can invoke clive
- `--conversational` mode exists but inner clive's stdin reading loop is a stub

---

## Workstream A: Remote Sandbox Boundaries

### Task A1: Sandbox wrapper script

**Files:**
- Create: `sandbox/run.sh`
- Create: `sandbox/profile.json`
- Test: `tests/test_sandbox.py`

**Step 1: Write the test**

```python
# tests/test_sandbox.py
import subprocess
import json
import os
import pytest

SANDBOX_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "sandbox", "run.sh")


def test_sandbox_script_exists():
    assert os.path.isfile(SANDBOX_SCRIPT)


def test_sandbox_blocks_write_outside_workdir(tmp_path):
    """Sandbox must prevent writes outside the session directory."""
    result = subprocess.run(
        ["bash", SANDBOX_SCRIPT, str(tmp_path), "touch /tmp/escape_test"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0 or not os.path.exists("/tmp/escape_test")


def test_sandbox_allows_write_inside_workdir(tmp_path):
    """Sandbox must allow writes inside the session directory."""
    result = subprocess.run(
        ["bash", SANDBOX_SCRIPT, str(tmp_path), f"touch {tmp_path}/inside_test"],
        capture_output=True, text=True, timeout=10,
    )
    assert os.path.exists(f"{tmp_path}/inside_test")


def test_sandbox_blocks_network_if_restricted(tmp_path):
    """If network=false in profile, outbound connections must fail."""
    result = subprocess.run(
        ["bash", SANDBOX_SCRIPT, str(tmp_path), "curl -s http://example.com",
         "--no-network"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0


def test_sandbox_profile_loading():
    """Profile JSON must parse and contain expected keys."""
    profile_path = os.path.join(os.path.dirname(__file__), "..", "sandbox", "profile.json")
    with open(profile_path) as f:
        profile = json.load(f)
    assert "fs_writable" in profile
    assert "max_procs" in profile
    assert "max_memory_mb" in profile
    assert "network" in profile
    assert "allowed_commands" in profile
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_sandbox.py -v`
Expected: FAIL (sandbox/ doesn't exist)

**Step 3: Implement sandbox wrapper**

`sandbox/run.sh` — a portable wrapper that uses `bwrap` (Linux) or falls back to `sandbox-exec` (macOS) or basic `ulimit` + `chroot` as last resort:

```bash
#!/usr/bin/env bash
# sandbox/run.sh — Execute a command inside a restricted sandbox.
# Usage: sandbox/run.sh <workdir> <command...> [--no-network]
#
# Provides:
#   - Filesystem: read-only root, writable workdir only
#   - Process: limited to 64 processes
#   - Memory: limited to 512MB (configurable via CLIVE_SANDBOX_MEM_MB)
#   - Network: allowed by default, --no-network to restrict
#   - No access to host home directory or credentials

set -euo pipefail

WORKDIR="$1"; shift
NO_NETWORK=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--no-network" ]; then
        NO_NETWORK=true
    else
        ARGS+=("$arg")
    fi
done

MEM_MB="${CLIVE_SANDBOX_MEM_MB:-512}"
MAX_PROCS="${CLIVE_SANDBOX_MAX_PROCS:-64}"

# Ensure workdir exists
mkdir -p "$WORKDIR"

if command -v bwrap &>/dev/null; then
    # Linux: bubblewrap — gold standard
    BWRAP_ARGS=(
        --ro-bind / /
        --bind "$WORKDIR" "$WORKDIR"
        --tmpfs /tmp
        --dev /dev
        --proc /proc
        --unshare-pid
        --unshare-uts
        --die-with-parent
    )
    if $NO_NETWORK; then
        BWRAP_ARGS+=(--unshare-net)
    fi
    # Hide host credentials
    BWRAP_ARGS+=(--tmpfs "$HOME/.ssh" --tmpfs "$HOME/.aws" --tmpfs "$HOME/.config")

    exec bwrap "${BWRAP_ARGS[@]}" \
        /bin/bash -c "ulimit -u $MAX_PROCS; ulimit -v $((MEM_MB * 1024)); cd $WORKDIR && ${ARGS[*]}"

elif [ "$(uname)" = "Darwin" ]; then
    # macOS: sandbox-exec with a custom profile (limited but better than nothing)
    PROFILE="(version 1)
(allow default)
(deny file-write* (subpath \"/\") (require-not (subpath \"$WORKDIR\")))
(deny file-write* (subpath \"/\") (require-not (subpath \"/tmp\")))
(deny file-write* (subpath \"/\") (require-not (subpath \"/dev\")))
"
    if $NO_NETWORK; then
        PROFILE+="(deny network*)"
    fi
    exec sandbox-exec -p "$PROFILE" \
        /bin/bash -c "ulimit -u $MAX_PROCS; cd $WORKDIR && ${ARGS[*]}"

else
    # Fallback: ulimit only (minimal protection)
    exec /bin/bash -c "ulimit -u $MAX_PROCS; ulimit -v $((MEM_MB * 1024)); cd $WORKDIR && ${ARGS[*]}"
fi
```

`sandbox/profile.json` — default sandbox profile:

```json
{
    "fs_writable": ["$WORKDIR", "/tmp"],
    "max_procs": 64,
    "max_memory_mb": 512,
    "max_disk_mb": 1024,
    "network": true,
    "allowed_commands": ["*"],
    "blocked_commands": ["rm -rf /", "mkfs", "dd", "shutdown", "reboot"],
    "timeout_seconds": 300
}
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/martintreiber/Documents/Development/clive && python -m pytest tests/test_sandbox.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sandbox/ tests/test_sandbox.py
git commit -m "feat(sandbox): add sandbox wrapper with fs/process/network isolation"
```

---

### Task A2: Integrate sandbox into executor command dispatch

**Files:**
- Modify: `executor.py` (the `_execute_command` or command dispatch section)
- Modify: `models.py` (add `sandboxed: bool` to Subtask or PaneInfo)
- Test: `tests/test_executor_sandbox.py`

**Step 1: Write the test**

```python
# tests/test_executor_sandbox.py
from executor import _wrap_for_sandbox

def test_sandbox_wrapping_adds_script_prefix():
    cmd = "ls -la"
    wrapped = _wrap_for_sandbox(cmd, "/tmp/clive/abc123", sandboxed=True)
    assert "sandbox/run.sh" in wrapped
    assert "/tmp/clive/abc123" in wrapped

def test_sandbox_wrapping_passthrough_when_disabled():
    cmd = "ls -la"
    wrapped = _wrap_for_sandbox(cmd, "/tmp/clive/abc123", sandboxed=False)
    assert wrapped == cmd
```

**Step 2: Run test, verify fail**

**Step 3: Add `_wrap_for_sandbox()` to executor.py**

In the command dispatch path, wrap commands through `sandbox/run.sh` when `CLIVE_SANDBOX=1` env var is set or when the pane is a remote-user session.

**Step 4: Run test, verify pass**

**Step 5: Commit**

```bash
git add executor.py models.py tests/test_executor_sandbox.py
git commit -m "feat(sandbox): integrate sandbox into executor command dispatch"
```

---

### Task A3: Per-user resource quotas

**Files:**
- Create: `sandbox/quotas.py`
- Test: `tests/test_quotas.py`

Implement a `UserQuota` dataclass that tracks per-SSH-user:
- Cumulative token usage (across all sessions)
- Concurrent session count
- Disk usage in session dirs
- Wall-clock time

Read from `~/.clive/quotas.yaml` (configurable per-user). Enforce at session start and during execution. Default: 100k tokens/day, 3 concurrent sessions, 1GB disk, 1h wall time.

**Step 1: Write the test**

```python
# tests/test_quotas.py
from sandbox.quotas import UserQuota, check_quota, DEFAULT_QUOTAS

def test_default_quotas_exist():
    q = DEFAULT_QUOTAS
    assert q.max_tokens_per_day == 100_000
    assert q.max_concurrent == 3
    assert q.max_disk_mb == 1024
    assert q.max_wall_seconds == 3600

def test_check_quota_passes_under_limit():
    q = UserQuota(max_tokens_per_day=1000, max_concurrent=2, max_disk_mb=100, max_wall_seconds=60)
    result = check_quota(q, tokens_used=500, concurrent=1, disk_mb=50, wall_seconds=30)
    assert result.allowed

def test_check_quota_fails_over_token_limit():
    q = UserQuota(max_tokens_per_day=1000, max_concurrent=2, max_disk_mb=100, max_wall_seconds=60)
    result = check_quota(q, tokens_used=1500, concurrent=1, disk_mb=50, wall_seconds=30)
    assert not result.allowed
    assert "token" in result.reason.lower()
```

**Step 2-5:** Implement, test, commit.

---

### Task A4: SSH server hardening config

**Files:**
- Create: `sandbox/sshd_clive.conf`
- Create: `docs/deployment/remote-setup.md`

Provide a hardened `sshd_config` snippet for the clive user:
- `ForceCommand` → `sandbox/run.sh` wrapping clive
- `AllowTcpForwarding no`
- `X11Forwarding no`
- `PermitTunnel no`
- `AcceptEnv ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY LLM_PROVIDER AGENT_MODEL`
- `ChrootDirectory` pointing to a jailed fs (optional, docs explain trade-offs)
- `MaxSessions 3` per user

Document the full remote setup in `docs/deployment/remote-setup.md`.

---

## Workstream B: Multi-Instance Server Mode

### Task B1: Request queue with file-based job store

**Files:**
- Create: `server/queue.py`
- Test: `tests/test_queue.py`

**Step 1: Write the test**

```python
# tests/test_queue.py
import os, tempfile
from server.queue import JobQueue, Job, JobStatus

def test_enqueue_and_dequeue(tmp_path):
    q = JobQueue(str(tmp_path))
    job = q.enqueue(task="hello world", user="testuser", toolset="minimal")
    assert job.status == JobStatus.PENDING
    assert os.path.exists(os.path.join(str(tmp_path), f"{job.id}.json"))

    next_job = q.dequeue()
    assert next_job is not None
    assert next_job.id == job.id
    assert next_job.status == JobStatus.RUNNING

def test_dequeue_empty(tmp_path):
    q = JobQueue(str(tmp_path))
    assert q.dequeue() is None

def test_fifo_ordering(tmp_path):
    q = JobQueue(str(tmp_path))
    j1 = q.enqueue(task="first", user="a", toolset="minimal")
    j2 = q.enqueue(task="second", user="a", toolset="minimal")
    got = q.dequeue()
    assert got.id == j1.id

def test_job_completion(tmp_path):
    q = JobQueue(str(tmp_path))
    job = q.enqueue(task="test", user="a", toolset="minimal")
    q.dequeue()
    q.complete(job.id, result="done", status=JobStatus.COMPLETED)
    loaded = q.get(job.id)
    assert loaded.status == JobStatus.COMPLETED
    assert loaded.result == "done"

def test_concurrent_dequeue_no_double_dispatch(tmp_path):
    """Two workers dequeueing simultaneously must not get the same job."""
    import threading
    q = JobQueue(str(tmp_path))
    q.enqueue(task="only one", user="a", toolset="minimal")

    results = []
    def worker():
        job = q.dequeue()
        results.append(job)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    non_none = [r for r in results if r is not None]
    assert len(non_none) == 1  # exactly one worker gets the job
```

**Step 2: Run test, verify fail**

**Step 3: Implement `server/queue.py`**

File-based job queue using `fcntl.flock` for atomic dequeue. Each job is a JSON file in the queue directory. Jobs are ordered by creation timestamp. The `dequeue()` method acquires a directory-level lock, picks the oldest PENDING job, marks it RUNNING, and releases. This is simple, requires no external dependencies, and survives process crashes (jobs stay PENDING if worker dies before completing).

```python
# server/queue.py
import fcntl
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    task: str
    user: str
    toolset: str
    status: JobStatus = JobStatus.PENDING
    result: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    worker_pid: int = 0
    session_dir: str = ""
    tokens_used: int = 0


class JobQueue:
    def __init__(self, queue_dir: str):
        self.queue_dir = Path(queue_dir)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.queue_dir / ".lock"

    def enqueue(self, task: str, user: str, toolset: str = "minimal") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], task=task, user=user, toolset=toolset)
        self._write_job(job)
        return job

    def dequeue(self) -> Job | None:
        with self._lock():
            pending = self._list_by_status(JobStatus.PENDING)
            if not pending:
                return None
            job = pending[0]  # FIFO by created_at
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            job.worker_pid = os.getpid()
            self._write_job(job)
            return job

    def complete(self, job_id: str, result: str, status: JobStatus = JobStatus.COMPLETED):
        job = self.get(job_id)
        if job:
            job.status = status
            job.result = result
            job.completed_at = time.time()
            self._write_job(job)

    def get(self, job_id: str) -> Job | None:
        path = self.queue_dir / f"{job_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        data["status"] = JobStatus(data["status"])
        return Job(**data)

    def _write_job(self, job: Job):
        path = self.queue_dir / f"{job.id}.json"
        data = asdict(job)
        data["status"] = job.status.value
        path.write_text(json.dumps(data, indent=2))

    def _list_by_status(self, status: JobStatus) -> list[Job]:
        jobs = []
        for path in sorted(self.queue_dir.glob("*.json")):
            if path.name == ".lock":
                continue
            data = json.loads(path.read_text())
            if data.get("status") == status.value:
                data["status"] = JobStatus(data["status"])
                jobs.append(Job(**data))
        jobs.sort(key=lambda j: j.created_at)
        return jobs

    def _lock(self):
        return _FileLock(self._lock_path)


class _FileLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self._fd = open(self.path, "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()
```

**Step 4-5:** Run tests, commit.

---

### Task B2: Worker pool daemon

**Files:**
- Create: `server/worker.py`
- Modify: `clive.py` (add `--serve` flag)
- Test: `tests/test_worker.py`

A worker process polls the job queue, picks a job, runs `clive.run()` in a subprocess (or in-process with isolated tmux session), and writes the result back. Multiple workers can run in parallel.

Key design decisions:
- Each worker gets its own tmux session (session name includes worker PID)
- Workers are forked processes (not threads) for isolation
- A supervisor (`--serve` mode) spawns N workers and restarts crashed ones
- Workers self-terminate after `--max-jobs` completions (prevents memory leaks from long-running LLM sessions)

```python
# server/worker.py (sketch)
import os, signal, subprocess, time
from server.queue import JobQueue, JobStatus

class Worker:
    def __init__(self, queue: JobQueue, max_jobs: int = 50):
        self.queue = queue
        self.max_jobs = max_jobs
        self.completed = 0
        self._running = True

    def run(self):
        signal.signal(signal.SIGTERM, self._handle_signal)
        while self._running and self.completed < self.max_jobs:
            job = self.queue.dequeue()
            if not job:
                time.sleep(1.0)
                continue
            try:
                result = self._execute(job)
                self.queue.complete(job.id, result=result, status=JobStatus.COMPLETED)
            except Exception as e:
                self.queue.complete(job.id, result=str(e), status=JobStatus.FAILED)
            self.completed += 1

    def _execute(self, job):
        """Run clive in a subprocess with isolated session."""
        # Subprocess ensures clean state per job
        result = subprocess.run(
            ["python3", "clive.py", "--quiet", "--json", "-t", job.toolset, job.task],
            capture_output=True, text=True,
            timeout=job.timeout or 300,
            cwd=os.path.dirname(__file__) + "/..",
        )
        return result.stdout

    def _handle_signal(self, signum, frame):
        self._running = False
```

---

### Task B3: Server supervisor with `--serve` CLI flag

**Files:**
- Modify: `clive.py` (add `--serve`, `--workers`, `--queue-dir` flags)
- Create: `server/supervisor.py`
- Test: `tests/test_supervisor.py`

The supervisor:
1. Creates the queue directory
2. Forks N worker processes
3. Monitors them (restart on crash)
4. Handles SIGTERM gracefully (drain queue, stop workers)
5. Logs to `~/.clive/server.log`

```bash
# Usage:
python clive.py --serve --workers 4 --queue-dir ~/.clive/queue
```

---

### Task B4: SSH integration — ForceCommand enqueues jobs

**Files:**
- Create: `server/ssh_entrypoint.sh`
- Modify: `sandbox/sshd_clive.conf`

When a remote user SSHs in, instead of running clive directly, the `ForceCommand` enqueues a job and tails the result:

```bash
#!/bin/bash
# server/ssh_entrypoint.sh — SSH ForceCommand for clive-as-a-service
TASK="$SSH_ORIGINAL_COMMAND"
if [ -z "$TASK" ]; then
    echo "Usage: ssh clive@host 'your task here'"
    exit 1
fi

# Enqueue and wait
JOB_ID=$(python3 -c "
from server.queue import JobQueue
q = JobQueue('$HOME/.clive/queue')
j = q.enqueue(task='$TASK', user='$USER', toolset='${CLIVE_TOOLSET:-minimal}')
print(j.id)
")

echo "Job $JOB_ID queued. Waiting for result..."

# Poll for completion
while true; do
    STATUS=$(python3 -c "
from server.queue import JobQueue, JobStatus
q = JobQueue('$HOME/.clive/queue')
j = q.get('$JOB_ID')
if j and j.status in (JobStatus.COMPLETED, JobStatus.FAILED):
    print(j.result)
    exit(0 if j.status == JobStatus.COMPLETED else 1)
else:
    exit(2)
" 2>/dev/null)
    RC=$?
    if [ $RC -ne 2 ]; then
        echo "$STATUS"
        exit $RC
    fi
    sleep 1
done
```

---

### Task B5: Health endpoint and metrics

**Files:**
- Create: `server/health.py`
- Test: `tests/test_health.py`

Simple Unix socket or `/tmp/clive/health.json` file that the supervisor updates every 5s:

```json
{
    "status": "healthy",
    "workers": 4,
    "workers_busy": 2,
    "queue_depth": 3,
    "jobs_completed": 147,
    "jobs_failed": 2,
    "uptime_seconds": 3600,
    "total_tokens": 1250000
}
```

This is readable by monitoring tools, load balancers, or `clive --status`.

---

## Workstream C: Safe Self-Modification Harness

### Task C1: Git-based atomic apply with auto-rollback

**Files:**
- Modify: `selfmod/workspace.py`
- Test: `tests/test_selfmod_workspace.py`

Currently `apply_changes()` writes files directly. Change it to:
1. Create a git branch `selfmod/{proposal_id}`
2. Apply changes on that branch
3. Run `python -m pytest tests/ -x` on the branch
4. If tests pass, merge to current branch
5. If tests fail, delete the branch and report failure

This ensures self-modifications never break the running code.

```python
# tests/test_selfmod_workspace.py
def test_apply_creates_branch(tmp_path):
    """apply_atomic must create a selfmod/ git branch."""
    # Set up a git repo in tmp_path, apply changes, verify branch exists
    ...

def test_apply_rolls_back_on_test_failure(tmp_path):
    """If tests fail after apply, the branch must be deleted and main untouched."""
    ...

def test_apply_merges_on_success(tmp_path):
    """If tests pass, changes must be merged into the working branch."""
    ...
```

---

### Task C2: Hot-reload mechanism for server mode

**Files:**
- Create: `server/reload.py`
- Test: `tests/test_reload.py`

When running in `--serve` mode and a self-modification is applied:
1. Supervisor detects file change (via `inotify`/`kqueue` or git hook)
2. Sends SIGUSR1 to workers
3. Workers finish current job, then self-restart (exec into new code)
4. Supervisor waits for all workers to restart before accepting new selfmod

```python
# tests/test_reload.py
def test_reload_signal_triggers_restart():
    """SIGUSR1 must cause worker to finish current job then restart."""
    ...

def test_no_reload_during_active_job():
    """Worker must not restart mid-job even if SIGUSR1 arrives."""
    ...
```

---

### Task C3: Separate LLM personas for proposer/reviewer/auditor

**Files:**
- Modify: `selfmod/proposer.py`
- Modify: `selfmod/reviewer.py`
- Modify: `selfmod/auditor.py`
- Test: `tests/test_selfmod_separation.py`

Currently all three roles use the same LLM call with different system prompts. Strengthen separation:
- Different model temperatures (proposer: 0.7, reviewer: 0.1, auditor: 0.0)
- Reviewer cannot see the proposer's reasoning (only the diff)
- Auditor cannot see reviewer's reasoning (only the verdict + diff)
- Add a `selfmod_model` config option to use a different model for review/audit

---

### Task C4: Eval-gated self-modification

**Files:**
- Modify: `selfmod/pipeline.py`
- Test: `tests/test_selfmod_eval_gate.py`

After the code gate passes but before merge, run the relevant eval layer:
1. Identify which evals are affected (e.g., change to `executor.py` → run Layer 2+3)
2. Run affected evals
3. Compare against baseline
4. Only merge if completion rate doesn't regress

```python
def test_selfmod_blocked_on_eval_regression():
    """Self-modification must be rejected if eval scores regress."""
    ...
```

---

## Workstream D: Local Parallel Coordination

### Task D1: Cross-process SharedBrain via Unix domain socket

**Files:**
- Create: `ipc.py`
- Modify: `pane_agent.py` (SharedBrain)
- Test: `tests/test_ipc.py`

Replace the in-memory `SharedBrain` dict with a Unix domain socket server that multiple clive instances can connect to. The first clive instance starts the server; subsequent ones connect.

```python
# tests/test_ipc.py
import threading
from ipc import SharedBrainServer, SharedBrainClient

def test_cross_process_fact_sharing(tmp_path):
    socket_path = str(tmp_path / "brain.sock")
    server = SharedBrainServer(socket_path)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()

    client1 = SharedBrainClient(socket_path)
    client2 = SharedBrainClient(socket_path)

    client1.post_fact("weather", "sunny")
    assert client2.get_fact("weather") == "sunny"

    server.shutdown()
```

---

### Task D2: Session discovery and collision avoidance

**Files:**
- Modify: `session.py`
- Create: `server/discovery.py`
- Test: `tests/test_discovery.py`

Before creating a tmux session, check for existing clive sessions on the same socket. If a pane name conflicts, either:
- Reuse the existing pane (if compatible toolset)
- Create with a unique suffix

Provide `clive --instances` to list all running clive sessions with their tasks and pane allocations.

---

### Task D3: Coordinated task splitting across local instances

**Files:**
- Create: `coordinator.py`
- Test: `tests/test_coordinator.py`

When a task is too large for one instance (e.g., "research 10 topics"), the planner can split it across multiple local clive instances. Each instance gets a subset of subtasks. Results are aggregated back.

Uses the job queue from Workstream B (even locally) to dispatch sub-tasks.

---

## Workstream E: Agent-to-Agent Protocol Completion

### Task E1: Wire TURN:waiting + QUESTION: into executor

**Files:**
- Modify: `executor.py` (the interactive worker loop, around line 300-400)
- Modify: `remote.py` (add `parse_question()`)
- Test: `tests/test_agent_conversation.py`

When the executor detects `TURN: waiting` on an agent pane, it must:
1. Parse the `QUESTION:` line
2. Send it to the outer LLM for an answer
3. Type the answer into the pane
4. Resume waiting for `TURN: thinking/done/failed`

```python
# tests/test_agent_conversation.py
from remote import parse_question

def test_parse_question():
    screen = "TURN: waiting\nQUESTION: What format should the output be in?"
    q = parse_question(screen)
    assert q == "What format should the output be in?"

def test_parse_question_none_when_no_question():
    screen = "TURN: thinking\nPROGRESS: step 1 of 3"
    q = parse_question(screen)
    assert q is None
```

---

### Task E2: Automatic file transfer after TURN:done

**Files:**
- Modify: `executor.py` (post-subtask cleanup)
- Test: `tests/test_agent_file_transfer.py`

When a remote agent subtask completes with `TURN: done` and `FILE:` declarations:
1. Parse the file list
2. SCP each file to the local session dir
3. Make them available to subsequent subtasks via `result_files` registry

---

### Task E3: Inner clive conversational loop

**Files:**
- Modify: `clive.py` (the `--conversational` code path)
- Test: `tests/test_conversational_loop.py`

Complete the stub: when `--conversational` and stdin is not a TTY, clive should:
1. Read a task from stdin
2. Execute it
3. Emit `TURN: done` + `CONTEXT: {...}` + `DONE: {...}`
4. Wait for next input on stdin
5. Support `TURN: waiting` + `QUESTION:` for asking the caller

```python
# tests/test_conversational_loop.py
import subprocess

def test_conversational_single_task():
    proc = subprocess.Popen(
        ["python3", "clive.py", "--conversational", "-t", "minimal"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    proc.stdin.write("echo hello\n")
    proc.stdin.flush()
    # Read until DONE:
    output = ""
    for line in proc.stdout:
        output += line
        if line.strip().startswith("DONE:"):
            break
    proc.terminate()
    assert "DONE:" in output
```

---

### Task E4: Agent authentication and authorization

**Files:**
- Create: `server/auth.py`
- Modify: `agents.py`
- Test: `tests/test_agent_auth.py`

Beyond SSH keys, add a token-based auth layer:
- Remote clive instances can require a bearer token
- Tokens stored in `~/.clive/agents.yaml` per host
- Token is forwarded as an env var (`CLIVE_AUTH_TOKEN`)
- Remote clive validates token before accepting tasks

---

### Task E5: Timeout and retry for remote agents

**Files:**
- Modify: `executor.py` (agent pane handling)
- Test: `tests/test_agent_timeout.py`

Add configurable timeouts per agent host:
- Connection timeout (already exists: `connect_timeout`)
- Task timeout (new: `task_timeout`, default 300s)
- Stall detection (no TURN:/PROGRESS: for N seconds → retry or fail)
- Retry policy: 0-2 retries with exponential backoff

---

## Implementation Order and Dependencies

```
Phase 3a (Foundation — do first):
  A1 → A2 → A3    Sandbox (independent)
  B1 → B2 → B3    Queue + Workers (independent)
  C1              Atomic apply (independent)

Phase 3b (Integration — needs 3a):
  A4              SSH hardening (needs A1-A3)
  B4 → B5         SSH entrypoint + health (needs B1-B3, A4)
  C2              Hot-reload (needs B2-B3, C1)
  D1              Cross-process brain (independent)

Phase 3c (Completion — needs 3b):
  C3 → C4         Selfmod personas + eval gate (needs C1-C2)
  D2 → D3         Discovery + coordination (needs D1, B1)
  E1 → E2         Agent conversation + file transfer (independent)
  E3              Conversational loop (needs E1)
  E4 → E5         Auth + timeout (needs E1-E3)
```

```
                    ┌─────────┐
                    │  A1-A3  │ Sandbox
                    │ Sandbox │
                    └────┬────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         ┌────────┐ ┌────────┐ ┌────────┐
         │   A4   │ │ B4-B5  │ │   C2   │
         │SSH Hard│ │SSH+Hlth│ │Hot Rld │
         └────────┘ └───┬────┘ └───┬────┘
                        │          │
                    ┌───┴──────────┴───┐
                    │    C3 → C4       │
                    │ Selfmod Personas │
                    └──────────────────┘

  ┌─────────┐           ┌─────────┐
  │  B1-B3  │           │  D1     │
  │Queue+Wkr│           │IPC Brain│
  └────┬────┘           └────┬────┘
       │                     │
       └──────────┬──────────┘
                  ▼
            ┌──────────┐
            │  D2 → D3 │
            │Discovery │
            └──────────┘

  ┌─────────────────┐
  │    E1 → E2      │
  │Agent Conversation│
  └───────┬─────────┘
          ▼
  ┌───────────────┐
  │   E3 → E4-E5  │
  │Conv Loop+Auth │
  └───────────────┘
```

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| bwrap not available on macOS | Sandbox degrades to ulimit-only | Accept; document clearly; recommend Linux for production |
| File-based queue doesn't scale past ~100 jobs/min | Latency under load | Design for SQLite upgrade path; keep interface abstract |
| Self-modification breaks running server | Downtime | Git-branch isolation + test gate + hot-reload |
| Cross-process SharedBrain socket dies | Agents lose shared state | Auto-reconnect + file-based fallback |
| Remote agent stalls indefinitely | Blocked pane, wasted tokens | Stall detection + hard timeout + SIGKILL |
| Multiple clive instances exhaust tmux panes | System resource limits | Session discovery + pane reuse + configurable max |

## Success Criteria

1. **Sandbox**: A remote user via SSH cannot read/write outside their session directory; cannot fork-bomb; cannot exhaust memory. Verified by adversarial test suite.
2. **Multi-instance**: `clive --serve --workers 4` handles 10 concurrent SSH task submissions without race conditions or data loss.
3. **Self-modification**: A selfmod that breaks tests is automatically rolled back. A selfmod that passes tests is atomically applied and hot-reloaded.
4. **Parallel local**: Two local clive instances can share facts via SharedBrain and don't collide on tmux sessions.
5. **Agent protocol**: `clive@remote "summarize this paper"` completes end-to-end including file transfer back, with proper timeout handling.
