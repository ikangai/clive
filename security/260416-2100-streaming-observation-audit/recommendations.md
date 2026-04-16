# Recommendations (Priority Order)

## Priority 1 — High (Fix Before Merging)

### 1. Explicit FIFO mode 0o600 on all `os.mkfifo` call sites

**Addresses:** F-1 (High), F-7 (Low — same root cause)
**Effort:** 3 lines, 3 files
**Risk:** None — reduces permissions, does not add capabilities

**Before (`src/clive/session/session.py:204`):**

```python
os.mkfifo(fifo_path)   # default umask → 0o644 on typical systems
```

**After:**

```python
os.mkfifo(fifo_path, mode=0o600)   # owner-only read+write
```

Same change in `evals/observation/latency_bench.py:66` (oracle FIFO) and `:182` (phase1 FIFO).

**Why 0o600 specifically:**
- Owner (clive's process) reads and writes.
- Group and other have no access.
- Independent of the user's umask.
- Does not break `tmux pipe-pane`'s `cat > {fifo}` because tmux runs as the same user.

**Verification:**
```bash
# Run a bench or start a session, then:
ls -l /tmp/clive/*/pipes/*.fifo
# Expected: prw-------  (1 rw-- bits for owner, nothing else)
```

## Priority 2 — Medium (Fix This Sprint)

### 2. Quote `fifo_path` before interpolating into the `pipe-pane` shell command

**Addresses:** F-3
**Effort:** 2 lines (import + call), 1 file

```python
import shlex
# session.py:209
pane_info.pane.cmd("pipe-pane", "-o", f"cat > {shlex.quote(fifo_path)}")
```

**Why not auto-fix:** Currently unreachable with shipped toolsets. Better landed as part of a toolset-validation change that also checks `pane_info.name` is a safe identifier.

### 3. `mode=0o700` on `/tmp/clive/` directory creation paths

**Addresses:** F-4 (partial — also mitigates parts of F-5)
**Effort:** per call site (~5 locations globally)
**Scope:** This branch's `session.py` plus pre-existing `tui_task_runner.py`, `llm_runner.py`, `runtime.py`, `cli_modes.py`.

Use the existing pattern from `networking/agents.py:50`:

```python
os.makedirs(fifo_dir, exist_ok=True, mode=0o700)
```

And consider an ownership check when the directory already exists:

```python
st = os.stat(fifo_dir)
if st.st_uid != os.getuid():
    raise RuntimeError(f"{fifo_dir} not owned by current user")
```

**Why not auto-fix in this audit:** Spans modules outside this branch's scope; belongs in a holistic `/tmp/clive/` hardening pass.

## Priority 3 — Low (Plan for Next Sprint)

### 4. Replace `assert` with explicit raise in `from_fifo_path`

**Addresses:** F-6
**Effort:** 2 lines

```python
# fifo_stream.py:51
if not os.path.exists(fifo_path):
    raise FileNotFoundError(fifo_path)
```

### 5. Add debug-level accept logging to `SpeculationScheduler.try_consume`

**Addresses:** F-12
**Effort:** 1 line

```python
# speculative.py inside try_consume after accept
log.debug(
    "speculation accepted v=%d age=%.2fs trigger=%s",
    call.version, time.monotonic() - call.started_at, call.trigger.kind,
)
```

Keeps default log volume unchanged; enables per-decision debugging when needed.

### 6. Monitor F-2 behavior via scheduler counters

No code change. During the Phase 2 observation period, track `discards_snapshot_mismatch` vs `accepts_total`. If a high fraction of accepts turn out to produce incorrect LLM behavior (observed manually or via user reports), revisit the prefix-check semantic in `_snapshot_matches`.
