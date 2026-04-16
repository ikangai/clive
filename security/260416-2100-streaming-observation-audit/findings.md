# Findings — Streaming Observation Branch

Ordered by severity. All findings verified by reading the code on commit `75873aa`.

---

## [HIGH] F-1 — FIFO created with default umask permissions (world-readable)

- **OWASP:** A01 (Broken Access Control) / A02 (Cryptographic Failures — secrets in plaintext on readable IPC)
- **STRIDE:** Information disclosure
- **Location:** `src/clive/session/session.py:204`
- **Confidence:** **Confirmed**

### Description

`os.mkfifo(fifo_path)` is called without an explicit `mode=` argument. The FIFO inherits the process umask. On macOS and most Linux distros the default umask is `0o022`, which gives the FIFO permissions `0o666 & ~0o022 = 0o644` — **readable by any user on the host**.

The FIFO carries every byte tmux renders from the pane: command outputs, file contents the user views, `sudo` password prompts the LLM responds to, API tokens in env dumps, scrollback from any prior command. All of it streams through this file in real time.

### Attack scenario

Attacker has a shell on the same machine (no elevated privileges needed):

```bash
# Wait for a clive session to start
while true; do ls /tmp/clive/*/pipes/*.fifo 2>/dev/null && break; sleep 1; done
# Read pane bytes in real time
cat /tmp/clive/*/pipes/*.fifo
```

Anything Clive drives through that pane is visible to the attacker: shell commands, outputs, sudo prompts with typed passwords, `cat ~/.aws/credentials` contents, `env | grep TOKEN` output.

### Code evidence

```python
# src/clive/session/session.py:193-209
base = session_dir or "/tmp/clive"
fifo_dir = os.path.join(base, "pipes")
fifo_path = os.path.join(fifo_dir, f"{pane_info.name}.fifo")
...
os.makedirs(fifo_dir, exist_ok=True)
if os.path.exists(fifo_path):
    os.unlink(fifo_path)
os.mkfifo(fifo_path)   # ← no mode=0o600
```

Verified on current process via shell:

```
$ umask
0022
$ touch /tmp/t && ls -l /tmp/t && rm /tmp/t
-rw-r--r--  1 ... /tmp/t
```

Default mode `0o644` — other users can read.

### Mitigation

```python
os.mkfifo(fifo_path, mode=0o600)
```

Single-line fix, owner-only read+write. The FIFO parent directory (`/tmp/clive/{session_id}/pipes/`) should also be 0o700, but that's a separate concern (the `os.makedirs` elsewhere creates `/tmp/clive/` with default perms as well).

---

## [MEDIUM] F-2 — Snapshot-prefix check accepts speculations on stale context

- **OWASP:** A04 (Insecure Design)
- **STRIDE:** Tampering (state consistency)
- **Location:** `src/clive/execution/speculative.py:150-155`, `src/clive/execution/interactive_runner.py:182`
- **Confidence:** **Confirmed** (design behavior)

### Description

`SpeculationScheduler.try_consume` accepts a speculative reply when `call.messages_snapshot == current_messages[:len(snap)]` — a **prefix** check. At fire time, `_spec_watch` captures `list(messages)` before the runner has appended the turn's screen diff or any `[EXIT:N]`/`[INTERVENTION:...]` notices. When the runner reaches `try_consume`, those appended entries are **after** the snapshot boundary, so the prefix check passes — and a speculation generated without that critical context is used as the reply.

### Attack scenario

```
T=0   Runner: send "rm -rf /some/path" (wrapped)
T=0.2 Pane: command runs, exits with code 1 (permission denied)
T=0.2 FIFO: "...EXIT:1 ___DONE_xxx___..."
T=0.2 _spec_watch sees cmd_end, snapshots messages (no [EXIT:1] yet!)
T=0.2 Scheduler fires chat_stream with stale snapshot
T=0.5 Runner: wait_for_ready returns with detection="marker"
T=0.5 Runner: _parse_exit_code → 1; appends "[EXIT:1]" user notice
T=0.5 Runner: next turn top, captures screen, appends diff
T=0.8 Speculative call completes: reply = "Good, removal complete. Now let's..."
T=0.8 Runner: try_consume — snapshot is a prefix of current → accept
T=0.8 Runner uses reply; extracts and sends the next command as if rm succeeded
```

Impact: irreversible actions based on wrong success assumption. Whether this is exploitable or just unreliable depends on what the LLM speculates; the mechanism cannot distinguish "correct guess" from "wrong guess on missing failure context."

Protection: `CLIVE_SPECULATE=1` is required to enable. Default-off.

### Code evidence

```python
# src/clive/execution/speculative.py:150
def _snapshot_matches(self, snap, current) -> bool:
    if len(snap) > len(current):
        return False
    return current[: len(snap)] == snap    # ← prefix-only, not equality
```

```python
# src/clive/execution/interactive_runner.py:173-184
async def _spec_watch():
    q = pane_info.stream.subscribe()
    while True:
        evt = await q.get()
        if evt.kind in SPEC_TRIGGERS:
            scheduler.fire(evt, messages_snapshot=list(messages))  # ← shallow copy at fire
```

### Mitigation (design-level, deferred)

Three options, in order of safety:

1. **Equality check instead of prefix.** Speculation only fires when the snapshot happens to become the exact messages at consume time. Kills most speculation wins; technically correct.
2. **Include pending annotations in the snapshot.** Have `_spec_watch` wait a brief moment after the event to let the runner append `[EXIT:N]` / `[INTERVENTION:...]` before snapshotting. Requires coordination with the runner thread.
3. **Narrow triggers to "safe" kinds only.** Specifically: fire only on intervention prompts (`password_prompt`, `confirm_prompt`) where the correct reply is narrowly bounded. Skip `cmd_end` and `error_keyword` where the reply depends on full context.

Today the mitigation is operational: `CLIVE_SPECULATE=0` default + metric counters at teardown. Fix is deferred per the disposition in `phase1-report.md` and design-doc §8.3.

---

## [MEDIUM] F-3 — Shell interpretation of FIFO path in `pipe-pane` command

- **OWASP:** A03 (Injection)
- **STRIDE:** Tampering / Elevation of Privilege (latent)
- **Location:** `src/clive/session/session.py:209`
- **Confidence:** **Likely** (currently unreachable with built-in toolsets; reachable with any future user-defined toolset)

### Description

```python
pane_info.pane.cmd("pipe-pane", "-o", f"cat > {fifo_path}")
```

tmux's `pipe-pane` passes the third argument through `/bin/sh -c`. If `fifo_path` contains shell metacharacters — spaces, `;`, `&`, backticks, `$(...)` — they are interpreted by the shell, not treated as part of a filename.

`fifo_path` is built from:
- `session_dir` — typically `/tmp/clive/{hex uuid}`, safe today.
- `pane_info.name` — hardcoded toolset values (`shell`, `browser`, `data`, ...), safe today.

**Today this is unreachable.** But the code has no defense if any future toolset or user-defined tool names contain a space, quote, or metacharacter. A tool named `"my tool"` produces `fifo_path = /tmp/.../pipes/my tool.fifo`, and the shell interprets the space as an argument separator — the `cat` writes to `/tmp/.../pipes/my` and the shell treats `tool.fifo` as a second argument (harmless here, but the general pattern is unsafe).

A tool named `"shell; rm -rf /"` would cause the shell to run `rm -rf /`. No such toolset exists; the unchecked construction is the footgun.

### Mitigation

Either:

1. **Validate `pane_info.name` at toolset-definition time**: allow only `[a-zA-Z0-9_-]`.
2. **Quote the path** when interpolating into the shell command: `shlex.quote(fifo_path)` for the `cat > ...` argument.

Option 2 is the conservative defense-in-depth choice. One line:

```python
import shlex
pane_info.pane.cmd("pipe-pane", "-o", f"cat > {shlex.quote(fifo_path)}")
```

Not auto-fixed because the current code is not exploitable with the shipped toolsets; flagged for the next toolset-extension work.

---

## [MEDIUM] F-4 — Shared `/tmp/clive/` directory enables cross-instance squatting

- **OWASP:** A04 (Insecure Design)
- **STRIDE:** Denial of Service
- **Location:** `src/clive/session/session.py:193-204` (and the broader `/tmp/clive/` convention)
- **Confidence:** **Likely**

### Description

`_maybe_attach_stream` falls back to `/tmp/clive/` when `session_dir` is `None`. `os.makedirs("/tmp/clive/pipes", exist_ok=True)` accepts a pre-existing directory regardless of ownership. An attacker with a shell on the same host can:

1. Pre-create `/tmp/clive/pipes/` with attacker-owned mode `0o755`.
2. Pre-create `/tmp/clive/pipes/shell.fifo` owned by attacker.
3. Clive runs. `os.path.exists(fifo_path)` → True. `os.unlink(fifo_path)` → **fails with EPERM** (attacker owns the file).
4. Exception caught by silent-fallback handler. Clive falls back to polling observation.

### Impact

- Denial of service on the streaming observation feature for clive (fallback to polling still works, so the agent continues — just without the Phase 1 latency wins).
- Not a confidentiality breach on its own. But combined with F-1 (if the squatted FIFO is attacker-writable), it becomes a **fake-event injection vector**: attacker pre-creates a FIFO they control, clive fails to replace it, clive's pipe-pane writes go to attacker's FIFO, attacker's bytes flow into clive's classifier.

Wait — Step 3's unlink failing means we never get to `os.mkfifo`. The silent-fallback path sets `pane_info.stream = None`. So clive doesn't actually read the attacker's FIFO. Downgraded to DoS-only.

### Mitigation

Follow the `networking/agents.py` pattern (`os.makedirs(ctl_dir, exist_ok=True, mode=0o700)`) — already used in Clive for SSH control sockets. Apply the same to `/tmp/clive/` creation paths globally:

```python
os.makedirs(fifo_dir, exist_ok=True, mode=0o700)
```

And preferably check ownership if the dir already exists:

```python
st = os.stat(fifo_dir)
if st.st_uid != os.getuid():
    raise RuntimeError(f"{fifo_dir} not owned by current user")
```

Not auto-fixed: the `/tmp/clive/` convention spans multiple modules (`tui_task_runner.py`, `llm_runner.py`, `runtime.py`). A holistic fix belongs in a follow-up hardening pass, not a point change inside `_maybe_attach_stream`.

---

## [LOW] F-5 — `os.path.exists` + `os.unlink` + `os.mkfifo` TOCTOU

- **OWASP:** A04 (Insecure Design)
- **STRIDE:** Tampering
- **Location:** `session.py:202-204`, `latency_bench.py:64-66` and `:180-182`
- **Confidence:** **Possible**

Between the `os.path.exists` check and `os.mkfifo`, a local attacker who can write to the parent directory could plant a symlink or a replacement FIFO. In practice, if the parent directory is `0o700`-owned by clive (see F-4), this is unreachable. With current default-umask directory perms, it's a narrow race. Recommend using `os.open(path, O_CREAT|O_EXCL)` semantics where possible; mkfifo has no atomic replace primitive, so combined with F-4's mitigation this closes.

---

## [LOW] F-6 — `assert os.path.exists(fifo_path)` can be stripped by `-O`

- **Location:** `src/clive/observation/fifo_stream.py:51`
- **Confidence:** **Confirmed**
- **Impact:** under `python3 -O`, `from_fifo_path` proceeds to `os.open` on a nonexistent path → `FileNotFoundError` instead of an informative `AssertionError`. Not a security issue in practice (the failure is loud either way), but `assert` is not a proper guard.
- **Fix:** `if not os.path.exists(fifo_path): raise FileNotFoundError(fifo_path)`.

---

## [LOW] F-7 — Oracle FIFO in bench is world-readable

- **Location:** `evals/observation/latency_bench.py:61-66`
- **Confidence:** **Confirmed**
- **Impact:** same class as F-1, scoped to bench runs. Scenarios are synthetic (no real passwords), but the `password_prompt` scenario invokes `sudo -S` which produces a real `Password:` prompt. If run on a machine where sudo's prompt contains a real hostname/username, leaking that is Low impact.
- **Fix:** `os.mkfifo(p, mode=0o600)` (same pattern as F-1 fix).

---

## [LOW] F-8 — `ByteClassifier._stream_pos` grows unbounded

- **Location:** `src/clive/observation/byte_classifier.py:51,76`
- **Confidence:** **Confirmed**
- **Impact:** Python `int` is arbitrary precision, so no overflow. Memory use is negligible (one int per classifier, one dict with ≤9 entries). Cosmetic.
- **Fix:** not needed.

---

## [LOW] F-9 — `run_in_executor(None, ...)` uses the default thread pool

- **Location:** `src/clive/execution/speculative.py:219`
- **Confidence:** **Possible**
- **Impact:** the speculative chat call runs on the default executor pool (shared across the process). If many panes are speculating concurrently and the default pool is saturated (default size: `min(32, os.cpu_count()+4)`), speculation calls could queue behind other blocking work. Bounded by `MAX_IN_FLIGHT=2` per pane; still a shared-resource pattern worth noting.
- **Fix:** use a dedicated `ThreadPoolExecutor` per pane or bound the global pool. Low priority.

---

## [LOW] F-10 — `PaneLoop.submit` race with `PaneLoop.stop`

- **Location:** `src/clive/execution/pane_loop.py:57-69`
- **Confidence:** **Possible**
- **Impact:** between a thread's `self._stopped` check and its `run_coroutine_threadsafe` call, another thread can set `_stopped = True` and close the loop. Submit would then raise `RuntimeError: Event loop is closed` (loud), not hang silently. Acceptable.

---

## [LOW] F-11 — Subscriber list mutation during fan-out

- **Location:** `src/clive/observation/fifo_stream.py:56-87`
- **Confidence:** **Possible**
- **Impact:** `_read_loop` iterates `self.subscribers` and calls `q.put_nowait(ev)`. A concurrent `subscribe()` call (from `interactive_runner`'s `_spec_watch` or `_send_agent_command`) appends to the same list. CPython's list append is thread-safe under the GIL but iteration + append is not ordered — a newly appended queue may miss the currently-being-dispatched event. Not a correctness violation (new subscribers don't expect history); flagged for awareness.

---

## [LOW] F-12 — No audit log of speculation accepts/discards (Repudiation)

- **Location:** `src/clive/execution/speculative.py`
- **Confidence:** **Confirmed**
- **Impact:** scheduler counters are logged at INFO on teardown but individual speculation accept decisions are not logged. If speculation leads to a bad outcome (F-2), there is no per-decision audit trail to reconstruct what happened. Counters are aggregate.
- **Fix:** add `log.debug("speculation accepted v=%d age=%.2fs trigger=%s", ...)` inside `try_consume` on accept. Debug-level so it doesn't spam by default.

---

## [INFO] F-13 — `os.open(fifo, O_NONBLOCK)` inside coroutine

- **Location:** `fifo_stream.py:62`
- **Confidence:** **Possible**
- **Impact:** blocking syscall on event loop thread. For local FIFOs this is microseconds; negligible. Informational only.

---

## Coverage matrix

| STRIDE | Tested | Findings |
|--------|--------|----------|
| S | ✓ | F-1 (via writable FIFO), AP-1 in attack map |
| T | ✓ | F-2, F-3, F-5 |
| R | ✓ | F-12 |
| I | ✓ | F-1, F-7 |
| D | ✓ | F-4, AP-5 in attack map |
| E | ✓ | F-2, F-3 (latent) |

| OWASP | Tested | Findings |
|-------|--------|----------|
| A01 Broken Access Control | ✓ | F-1 |
| A02 Cryptographic Failures | ✓ | F-1 (plaintext on readable IPC) |
| A03 Injection | ✓ | F-3 |
| A04 Insecure Design | ✓ | F-2, F-4, F-5 |
| A05 Security Misconfiguration | ✓ | F-1, F-7 (default umask) |
| A06 Vulnerable Components | ✓ | no new deps added on this branch |
| A07 Auth & Identification | ✓ | N/A for this branch |
| A08 Software & Data Integrity | ✓ | F-2 (speculation over-trust) |
| A09 Logging & Monitoring | ✓ | F-12 |
| A10 SSRF | — | no server-side request flow in this branch |
