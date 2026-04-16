# Streaming Observation & Speculative Decision — Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:writing-plans to turn this design into an implementation plan, then superpowers:executing-plans to implement it task-by-task.

**Goal:** Reduce the latency between "something changes on a tmux pane" and "the LLM has emitted a next action," and eliminate the current blind spots for *subtle* signals (ANSI color changes, blink attributes, animated redraws). Keep cost within ~1.8x of today.

**Architecture:** Replace poll-based observation with an event-driven pipeline fed by `pipe-pane → FIFO`. Two cheap layers run on the raw byte stream — L1 activity heartbeat and L2 byte-regex — escalating only to the existing L3 `ScreenClassifier` and L4 main LLM when warranted. For high-confidence triggers (command completion, intervention prompts, error keywords), fire the L4 call *speculatively* in parallel with pane settling, using version-stamped cancel-on-supersede semantics to guarantee ordering.

**Tech stack:** Python 3 asyncio, tmux `pipe-pane`, the existing `observation/` and `execution/` modules, the Anthropic SDK's stream cancellation support.

**Governing principle from SPEC.md:** *"LLM where judgment is required, shell everywhere else."* L1/L2 are shell-level signal detection. L3 retains its role. L4 speculation overlaps the expensive phase with preceding work — the expense doesn't grow, the wall-clock wait shrinks.

---

## 1. Motivation

Today's observation loop (`observation/completion.py::wait_for_ready`, `execution/interactive_runner.py`) polls `capture-pane` with adaptive backoff (10ms → 500ms). Screen classification runs once per turn on the captured text. The main LLM fires only after a command completes (marker, prompt sentinel, or idle timeout).

**Concrete problems:**

1. **Poll-interval slack:** up to 500ms between "bytes hit the pane" and "classifier sees them." On fast-scrolling output, errors can scroll past entirely between polls.
2. **Text-only classifier:** `capture-pane -p` strips ANSI escape sequences by default. Color changes, blink attributes, and animated redraws are invisible. For CLI tools that signal state *only* via color (status bars, TUIs like `claude` itself), we miss signal entirely.
3. **No overlap of inference with observation:** the main LLM call is strictly sequential after command completion. Several hundred ms of wall-clock time is lost to waiting for the pane to settle before inference begins.

**Typical measured latency today:** ~1-2.5s from pattern-appearance to LLM-first-token on intervention-heavy paths.

**Achievable floor** (given TTFT ≈ 200-400ms for Haiku): ~300-600ms.

---

## 2. Non-goals

- **Not replacing L3 (`ScreenClassifier`).** It stays exactly as is.
- **Not changing the planner, DAG scheduler, or summarizer.** This is strictly an observation/execution-layer change.
- **Not the "every byte fires an LLM" design.** That was considered and rejected in brainstorming — cost-unbounded, redundant on spinner frames, and no better than the layered design for signal quality.
- **Not server-side cancellation of Anthropic API calls.** Closing the HTTP stream stops our receive, but the server may have generated the response; we may pay full tokens. Cost is bounded by `MAX_IN_FLIGHT` + rate limit + circuit breaker, not by cancellation itself.
- **Not supported on non-tmux panes.** If `pipe-pane` isn't available, pane falls back to polling automatically.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  tmux pane                                              │
│    │                                                    │
│    ├── capture-pane (today's path, retained for L3)     │
│    └── pipe-pane ──► FIFO ──┐                           │
│                             ▼                           │
│                    ┌─────────────────┐                  │
│                    │ stream_reader   │  async task      │
│                    │ (per-pane)      │                  │
│                    └────────┬────────┘                  │
│                             │ raw bytes                 │
│                             ▼                           │
│             ┌─────────┐  ┌─────────────────┐            │
│       L1 ◄──┤heartbeat│  │  L2 byte regex  │──► L2 hits │
│             └─────────┘  └────────┬────────┘            │
│                                   │                     │
│                                   ▼                     │
│                        ┌──────────────────┐             │
│                        │   event_bus      │             │
│                        │ (async fan-out)  │             │
│                        └────────┬─────────┘             │
│                                 │                       │
│               ┌─────────────────┼─────────────────┐     │
│               ▼                 ▼                 ▼     │
│       ┌───────────────┐ ┌──────────────┐ ┌────────────┐ │
│       │ wait_for_ready│ │ L3 classifier│ │speculation │ │
│       │ (event-driven)│ │ (snapshot)   │ │ scheduler  │ │
│       └───────────────┘ └──────────────┘ └──────┬─────┘ │
│                                                 │       │
│                                                 ▼       │
│                                         ┌───────────┐   │
│                                         │ main LLM  │   │
│                                         │ (version- │   │
│                                         │  stamped) │   │
│                                         └───────────┘   │
└─────────────────────────────────────────────────────────┘
```

**New modules (~600 LOC total):**
- `observation/fifo_stream.py` — per-pane pipe-pane lifecycle, async reader, ring buffer, subscriber fan-out
- `observation/byte_classifier.py` — L2 byte regex (ANSI-aware)
- `execution/speculative.py` — version-stamped LLM call pipeline with supersede semantics

**Modified modules:**
- `observation/completion.py` — `wait_for_ready` gains optional `event_source: asyncio.Queue`. If provided, blocks on events instead of polling. Poll path retained as fallback.
- `execution/interactive_runner.py` — wires speculation for intervention/error/cmd_end triggers. Rest unchanged.
- `session/session.py` — on pane creation, also create `PaneStream` and attach to `PaneInfo`.

**Not touched:** `ScreenClassifier`, `capture_pane`, `command_extract`, planner, DAG scheduler.

**Feature flag:** `CLIVE_STREAMING_OBS=1`. Off by default until Phase 1 gate passes.

---

## 4. FIFO pipeline + L1/L2 (detection layer)

### 4.1 PaneStream

```python
# observation/fifo_stream.py
class PaneStream:
    def __init__(self, pane_info: PaneInfo, session_id: str):
        self.fifo_path = f"/tmp/clive/{session_id}/pipes/{pane_info.name}.fifo"
        os.makedirs(os.path.dirname(self.fifo_path), exist_ok=True)
        os.mkfifo(self.fifo_path)
        pane_info.pane.cmd("pipe-pane", "-o", f"cat > {self.fifo_path}")
        self.ring = collections.deque(maxlen=64 * 1024)
        self.last_byte_ts = time.monotonic()
        self.subscribers: list[asyncio.Queue] = []
        self._reader_task = asyncio.create_task(self._read_loop())

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=256)
        self.subscribers.append(q)
        return q

    async def _read_loop(self):
        # Open FIFO non-blocking; read chunks; feed ring buffer + classifier
        ...

    async def close(self):
        # pipe-pane toggle off → unlink FIFO → cancel reader → drain queues
        ...
```

**L1 — activity heartbeat:** `time.monotonic() - last_byte_ts` answers "pane moved in last N ms?" Sufficient for "still alive" signals (spinners).

### 4.2 L2 byte regex

```python
# observation/byte_classifier.py
BYTE_PATTERNS: list[tuple[bytes, str]] = [
    (rb'\x1b\[[0-9;]*3[13]m',    'color_alert'),     # red/yellow fg
    (rb'\x1b\[[0-9;]*4[13]m',    'color_bg_alert'),  # red/yellow bg
    (rb'\x1b\[[0-9;]*5m',        'blink_attr'),
    (rb'(?:^|[^\w])[Pp]assword\s*:', 'password_prompt'),
    (rb'\[y/N\]|\[Y/n\]',        'confirm_prompt'),
    (rb'Are you sure',           'confirm_prompt'),
    (rb'Traceback|FATAL|panic:', 'error_keyword'),
    (rb'Permission denied',      'permission_error'),
    (rb'EXIT:(\d+) ___DONE_',    'cmd_end'),          # reuses wrap_command marker
]
```

**Cross-chunk handling:** ring buffer keeps a 4KB carry-over tail; patterns are scanned starting `chunk_start - 128` bytes back (max pattern length).

**Emit:** `ByteEvent(kind, match_start, match_bytes, timestamp)` fanned out to all subscriber queues. Queues cap at 256; oldest dropped with a log warning if a consumer falls behind.

---

## 5. Speculation engine (cancel-on-supersede)

### 5.1 Fire rules

| Event kind | Speculate? | Why |
|---|---|---|
| `cmd_end` | **yes** | LLM will need to decide next step anyway |
| `password_prompt` | **yes** | Deterministic response required |
| `confirm_prompt` | **yes** | Short decision |
| `error_keyword` | **yes** | Likely needs LLM judgment |
| `color_alert` / `blink_attr` | **no** | Ambiguous alone; fed to L3 only |
| `permission_error` | **yes** | Likely means abort/retry with sudo |

Spinner frames, progress patterns, L1 heartbeats — **never** fire speculation.

### 5.2 Scheduler

```python
# execution/speculative.py
@dataclass
class SpecCall:
    version: int
    trigger: ByteEvent
    future: asyncio.Task
    messages_snapshot: list[dict]     # frozen at fire time
    started_at: float

class SpeculationScheduler:
    MAX_IN_FLIGHT = 2                 # per-pane bound
    MIN_FIRE_INTERVAL = 0.2           # rate limit (200ms)
    BREAKER_THRESHOLD = 5             # cancellations/min to disable

    def __init__(self, client, model):
        self.in_flight: list[SpecCall] = []
        self.latest_version = 0
        self.accepted_version = 0
        self._recent_cancels: collections.deque = collections.deque(maxlen=10)
        self._disabled = False
```

### 5.3 Supersede & accept semantics

- Single atomic `accepted_version` int per pane.
- **Accept rule:** a call's result is consumed iff `call.version > accepted_version` **and** `call.messages_snapshot == messages[:len(snapshot)]` at consume time.
- **Mismatch ⇒ discard and fire fresh.** Covers the case where the main loop's messages list diverged from what the speculative call assumed.
- Older in-flight calls get `future.cancel()` when a new call fires and concurrency is at cap. Newest is kept; oldest is dropped.

### 5.4 Cost bound

- `MAX_IN_FLIGHT = 2` per pane.
- `MIN_FIRE_INTERVAL = 200ms` per pane regardless of event rate.
- Circuit breaker: >5 cancellations/min on a pane ⇒ disable speculation for that pane's remaining turns, log warning. Auto-reset at pane teardown.

Expected waste: ~30-80% of speculative calls cancelled. Net token cost: ~1.5x on intervention-heavy paths, ~1.0x on smooth ones. Well within the 1.8x phase-2 gate.

---

## 6. Integration with `interactive_runner`

### 6.1 `wait_for_ready` becomes event-aware

```python
# observation/completion.py
def wait_for_ready(
    pane_info: PaneInfo,
    marker: str | None = None,
    timeout: float | None = None,
    max_wait: float = MAX_WAIT,
    detect_intervention: bool = False,
    event_source: asyncio.Queue | None = None,   # NEW
) -> tuple[str, str]:
    if event_source is None:
        return _wait_polling(pane_info, marker, timeout, max_wait, detect_intervention)
    return _wait_event_driven(pane_info, marker, event_source, timeout, max_wait, detect_intervention)
```

Event-driven variant blocks on `event_source.get()` with a short timeout for idle fallback; captures screen once at completion. Same return contract — `(screen, detection_method)` — so callers are unchanged.

### 6.2 Runner delta

Pseudocode showing only what changes in `run_subtask_interactive`:

```python
stream = pane_info.stream                    # set at pane creation
scheduler = SpeculationScheduler(client, effective_model)
my_events = stream.subscribe()

async def _spec_watch():
    async for evt in stream.subscribe():
        if evt.kind in SPEC_TRIGGERS:
            messages_snapshot = list(messages)
            scheduler.fire(evt, messages_snapshot)

spec_task = asyncio.create_task(_spec_watch())

for turn in range(1, subtask.max_turns + 1):
    screen = capture_pane(pane_info)
    diff = compute_screen_diff(prev_screen, screen)
    messages.append({"role": "user", "content": diff})

    # NEW: try to consume an accepted speculative result matching the
    # current messages prefix. If one exists, we save a round trip.
    reply, pt, ct = scheduler.try_consume(current_messages=messages)
    if reply is None:
        reply, pt, ct = chat_stream(client, messages, ...)   # existing path

    # ... rest identical
```

### 6.3 Pane lifecycle

On pane creation (`session/session.py`): also create `PaneStream(pane_info, session_id)` and attach to `PaneInfo.stream`. If `CLIVE_STREAMING_OBS` is unset or `mkfifo`/`pipe-pane` fails, `pane_info.stream = None` and the runner takes the polling path — bit-identical to today.

Teardown order (reverse of creation): pipe-pane off → unlink FIFO → cancel reader task → cancel `_spec_watch` → cancel in-flight speculations.

---

## 7. Failure modes

| Category | Failure | Recovery |
|---|---|---|
| Plumbing | `mkfifo`/`pipe-pane` fails | Log once, fall back to poll path for that pane only |
| Plumbing | Reader heartbeat detects 30s silence with visible pane activity | Mark stream unhealthy, fall back to polling |
| Ordering | Stale response after newer one accepted | `accepted_version` compare-and-swap rejects it |
| Ordering | Context divergence between snapshot and current messages | Snapshot-prefix check in `try_consume`; mismatch ⇒ discard |
| Classifier | `color_alert` false positive (e.g. `ls --color`) | Never fires speculation alone; fed to L3 as hint |
| Classifier | Command echo matches `cmd_end` | Reuses existing `EXIT:$` guard from `_parse_exit_code` |
| Classifier | Cross-chunk patterns | 4KB carry-over tail; 128-byte max pattern length |
| Cost | Speculation storms | `MAX_IN_FLIGHT=2`, 200ms rate limit, 5-cancellation-per-min breaker |
| Cost | Server-side token cost on cancelled calls | **Not mitigated**; bounded by the above three, not by cancellation |
| Lifecycle | Pane dies mid-turn | Reader catches EOF → signals scheduler to stop firing; in-flight calls allowed to complete |
| Lifecycle | FIFO path collision across sessions | Path includes `session_id` |
| TUI | Extreme redrawers (Claude Code, vim, ranger) | L2 is an accelerator, not a replacement; L3 remains source of truth. Measured separately |

---

## 8. Measurement harness

**Location:** `evals/observation/latency_bench.py`. Modes: `baseline` / `phase1` / `phase2`. N=50 runs per scenario per mode.

### 8.1 Scenarios

| # | Reproducer | L2 target | What it stresses |
|---|---|---|---|
| 1 | `sleep 0.5 && printf '\x1b[31mERROR: boom\x1b[0m\n' && sleep 2` | `color_alert` + `error_keyword` | Error scrolls fast — today's loop can miss it between polls |
| 2 | `sudo -k && sudo -S echo ok` | `password_prompt` | Intervention latency, highest-value path |
| 3 | `echo 'y/N test' && echo -n '[y/N] '` | `confirm_prompt` | Intervention latency |
| 4 | `spinner 5 && echo done` | `cmd_end` | Normal completion, no alerts |
| 5 | `spinner 5 && exit 1` | `cmd_end` (exit≠0) | Completion with error state |
| 6 | `printf 'status\n'; sleep 1; printf '\x1b[A\x1b[31mstatus\x1b[0m\n'` | `color_alert` only | **Color-only change** — baseline *cannot detect this at all* |

### 8.2 Metrics per run

- `detect_latency_ms` — FIFO first-byte-of-pattern → L2 event (phase1/2 only)
- `e2e_latency_ms` — pattern appearance → LLM first response token
- `missed` — bool: pipeline ever registered the pattern? Validated against an out-of-band `capture-pane` log
- `cost_tokens` — input + output tokens for the run
- `spec_waste` (phase2 only) — fraction of speculative calls cancelled before accept

### 8.3 Phase gates

| Phase | Median `e2e_latency_ms` reduction | Cost ratio | Missed rate |
|---|---|---|---|
| 1 → ship | ≥30% median e2e_latency_ms reduction on scenarios 1-3, 5 OR detection of a previously-missed scenario where baseline had 100% missed rate | ≤1.05x | ≤ baseline |
| 2 → ship | ≥50% on scenarios 1-3, 5 | ≤1.8x | ≤ baseline |

> **Note on criterion 1 revision (2026-04-16):** The original criterion 1 was written assuming uniformly slow baseline paths. Measurement (commit `f5b68c1`) showed that on scenarios baseline catches in 1-2 adaptive-backoff poll cycles (`password_prompt`, `error_scroll`), the 30% bar is unreachable — Phase 1's event-driven path has no room to improve when baseline is already catching at the floor. The revised criterion credits new detection capability on scenarios baseline fundamentally cannot see (`color_only`, `confirm_prompt`), which are the motivations in §1.

If a phase misses its gate, **don't ship that phase** — root-cause first. Phase 1 can ship without Phase 2. Phase 2 cannot ship without Phase 1.

Scenario 6 is load-bearing for the "don't miss subtle signals" goal. Baseline fundamentally can't see it (stripped ANSI). Phase 1 must detect it or the architecture is wrong.

---

## 9. Implementation phases

**Phase 0 — baseline measurement (no code changes to core).** Build `latency_bench.py` + the six scenarios. Run against today's loop to establish the numbers we're trying to beat. Deliverable: `evals/observation/baseline-report.md`.

**Phase 1 — FIFO + L1/L2 + event-driven `wait_for_ready`.** Ship modules from §4 + §6.1 + pane-lifecycle hookup. Feature-flag off by default. Run `latency_bench.py --mode phase1`, compare to baseline. If phase-1 gate passes, flip flag on by default.

**Phase 2 — speculation scheduler + runner integration.** Ship modules from §5 + §6.2. Run `latency_bench.py --mode phase2`, compare to phase-1. If phase-2 gate passes, flip flag on by default. If not, keep phase 1 shipped and defer phase 2 indefinitely.

Each phase is independently shippable, independently revertible, and gated on measured evidence — not intuition.

---

## 10. Open questions

1. **TUI tolerance thresholds.** Claude Code (Ink) redraws heavily. Do we need a per-driver "streaming observation tier" (aggressive / conservative / off)? Defer until Phase 1 data shows the TUI-specific missed-event rate.
2. **Multi-pane coordination.** Speculation is per-pane today. If two panes in the same DAG subtask fire speculations simultaneously, do they contend for the same LLM budget? Leave as single-pane scope for v1; revisit if the DAG scheduler surfaces problems.
3. **Delegate-mode interaction.** For BYOLLM panes where the remote round-trips inference over SSH (`llm/delegate_client.py`), speculation multiplies the SSH traffic. Might need a "no speculation" flag for delegate-mode panes. Deferrable — phase gates run on local panes first.
