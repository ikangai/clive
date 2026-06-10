# Land a structured turn-trace ledger in `execution/runtime.py` — every runner writes one JSONL record per (command, exit, classifier verdict, downstream-turn outcome) — as the substrate for every later observation/driver/mode-pick decision.

**Thesis:** Clive's architectural bottleneck is not "the classifier escalates too often"; it is that **no decision in the system is currently judged against what actually happened on a user's machine.** Every knob — classifier rules, driver RESPONSE FORMAT, mode picker, repair retries, speculation — is tuned against hand-written scenarios authored by the same person tuning the knob. The next concrete architectural step is the smallest thing that breaks that loop: a content-addressed turn-trace ledger emitted by all runners, with a stable schema, on by default, locally stored, opt-out per pane. Not telemetry. Not analytics. A first-class signal channel whose absence is the reason the eval bench has become an echo chamber.

## Locus

- **`src/clive/observation/trace.py`** (new, ~120 lines) — defines `TurnTrace` dataclass: `session_id`, `subtask_id`, `pane_id`, `mode`, `turn_idx`, `command` (or `command_hash` + length when redaction on), `exit_code`, `screen_bytes_hash`, `classifier_verdict` (event_type, needs_llm, summary), `decided_action` (one of: `accept`, `escalate_to_llm`, `repair_retry`, `complete`, `abort`), `downstream_exit_after_action`, `tokens_in`, `tokens_out`, `wall_ms`, `model_tier`. Plus a `TraceWriter` with line-buffered append to `~/.clive/traces/{YYYY-MM-DD}/{session}.jsonl`.
- **`src/clive/execution/runtime.py`** — single chokepoint every runner already crosses for command dispatch + observation. Add `RuntimeContext.emit_trace(trace: TurnTrace)`. Each runner (`script_runner.py`, `planned_runner.py`, `interactive_runner.py`, `toolcall_runner.py`) gets a 2-line call site after the classifier verdict + after the *next* turn's exit is known, so we capture the **pair** (verdict, downstream truth) — that pair is what the existing bench cannot generate.
- **`src/clive/session/session.py`** — register a writer per session in setup so the path is owned, flushed on shutdown, and rotates daily. ~10 lines.
- **`evals/observation/replay.py`** (new, ~60 lines) — read traces, regenerate `phase1-report.json`-shaped aggregates over real runs, not hand-written scenarios. This is the piece that closes the loop: the next classifier change is judged against the **previous month's traces**, not new scenarios the same author writes.

## Rationale tied to current state

Four signals from the brief and the codebase converge here:

1. The observation loop already produces a structured classifier verdict (`observation.py` returns an `EventType` + `needs_llm` + `summary`) — but it is consumed and discarded inside the runner's local scope. The single highest-leverage thing we own is currently thrown away every turn.
2. The brief calls out that **autoresearch's biggest win (+37pp) came from a measured driver experiment**. Driver experiments need a corpus of real turns to score against; we have none. Every future driver tweak will be argued from scenarios, not behavior.
3. The 3-tier router, the per-pane model tiers, and the speculative phase 2 all make implicit cost/quality tradeoffs whose calibration **requires longitudinal pair data** — verdict-vs-outcome — that no current path emits.
4. The framed nonce-authenticated remote protocol already serializes turn state for `clive@host`; the in-process trace is the same shape one level up. A unified schema means remote traces stream home for free in round 4.

This is the substrate move that makes the next five architectural decisions falsifiable instead of rhetorical.

## Tradeoffs accepted

- **Disk + privacy surface.** Local-only by default, env opt-out (`CLIVE_TRACE=0`), command field hashed when `CLIVE_TRACE_REDACT=1`, and a documented schema-versioning rule (`v=1`). No upload path in this PR — that is the next architectural decision and now has data to argue with.
- **Schema lock-in.** Once writers exist, the schema is load-bearing; the cost is borne now by versioning every record and reserving a `meta: dict` slot. Cheaper than the alternative (no signal at all).
- **One more thing every runner must remember to call.** Mitigated by routing through `runtime.py` — the one module every mode already depends on — and a runner-base helper so the omission becomes a test failure.
- **Not a user-visible win this PR.** Correctly so: this is the missing instrument, not the experiment. The next PR (classifier change, driver tweak, mode-pick heuristic — any of them) will run against real traces and produce an *honest* number. That is the architectural unlock; the headline metric is the next PR's, not this one's.

What this explicitly is not: a logging library, an analytics pipeline, a UI, or a new abstraction layer between runners and the classifier. It is one dataclass, one writer, one replay reader, and four 2-line call sites — sized exactly to the gap the current bench cannot fill.
