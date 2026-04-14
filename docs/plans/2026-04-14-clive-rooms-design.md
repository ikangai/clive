# Clive Rooms — Persistent Multi-Party Chat & Breakout Councils

> **For Claude:** When this document is ready for execution, wrap task breakdown in a separate plan file and invoke `superpowers:executing-plans` from there. This document is the design, not the execution plan.

**Goal.** Enable multiple clive instances (and humans) to participate in persistent, long-lived rooms hosted by an always-on broker, with round-robin turn discipline and first-class support for ephemeral private breakout councils. Rooms are named containers; **threads** are the unit of conversation inside them. Breakout councils are just threads with a restricted member list and `private: true` — one mechanism serves both use cases.

**Architecture.** A dedicated always-on host runs `clive@lobby`, a new `--role broker` mode of clive that swaps the planner/executor for a deterministic, single-threaded event-loop protocol service. Member clives connect via existing SSH agent-addressing. The lobby authenticates, enforces turn order, fans out messages, and snapshots threads to disk. An LLM is used only for rolling thread summarization.

**Tech stack.** Existing `networking/protocol.py` (extended with new frame kinds); existing SSH agent-addressing and named-instance registry; `selectors` stdlib for lobby IO; append-only JSONL snapshots.

**Governing principle from SPEC.md.** *"LLM where judgment is required, shell everywhere else."* The lobby is the one piece of the system that is explicitly *not* allowed to have judgment. LLM judgment lives in member clives; the lobby's sole LLM call-site is summarization.

---

## 1. Non-goals (v1)

- **Election / failover.** A single always-on broker is the v1 answer. If the lobby host is down, rooms are unavailable.
- **Pane-as-context for room participation.** Rooms deliberately break clive's usual "the pane scrollback IS the sub-agent's context" principle because a single member in multiple threads would see all threads' transcripts interleaved in one pane. Room context is delivered as structured payload inside `your_turn` frames; the scrollback is for human observability only.
- **Invite-only rooms.** All rooms are open-by-default within the lobby; SSH access to the lobby host is the only access gate.
- **Per-member identity beyond SSH.** No user accounts. If you can SSH to the lobby, you are trusted inside it.
- **Multi-broker federation.** One lobby per deployment.
- **Real-time UX guarantees.** 60-second auto-pass on turns is acceptable.
- **Late admission into private threads.** Private threads have fixed membership at `open_thread` time; rejoin-after-drop works but third-party join does not. A dedicated admission protocol can be added later.

---

## 2. Core concepts

### 2.1 Room

A named persistent container. Properties:

- **name** — short identifier, unique per lobby.
- **membership** — ephemeral; a member exists in a room only while its SSH session is alive and has issued `join_room`. SSH drop = member removed.
- **threads** — list of threads ever opened in this room, regardless of state.

Rooms are snapshotted to disk and survive lobby restarts. Membership is not snapshotted.

### 2.2 Thread

The unit of conversation. Properties:

- **thread_id** — assigned by the lobby (format `{room}-t{n}`), never by the client.
- **room** — the parent room.
- **initiator** — the member who opened it; initial owner.
- **members** — **ordered** list of rotation participants. Fixed at `open_thread` time for private threads; appendable by public-thread `join_thread` for latecomers.
- **private** — boolean; if `true`, the thread is completely invisible in `list_threads` to non-members — its id, members, and very existence are hidden.
- **state** — `open` | `dormant` | `closed`.
- **current_speaker** — rotation cursor; the member whose turn it currently is.
- **messages** — append-only log of `say` / `pass` events (persisted as JSONL).

Breakout councils are `private: true` threads. No separate concept.

### 2.3 Rotation and pass

Rotation is an ordered walk over `members`. Every turn ends with exactly one frame from `current_speaker` — either `say` (substantive) or `pass` ("nothing"). Both advance the cursor. Passing is per-turn: a member who passed on message 5 may speak substantively on message 6. The room driver instructs members to pass liberally; pass is the norm.

### 2.4 Session kind

Each SSH session to the lobby declares itself `clive` or `human` via a `session_hello` frame immediately after connection. The lobby uses this only to route message-injection semantics (§3.3); it is not a trust differentiator (SSH is).

---

## 3. Turn discipline

1. **Threads are the unit of conversation.** Turn discipline applies within a thread; a room with N open threads has N independent rotations.
2. **Only `current_speaker` may emit `say` or `pass`** in a given thread. Out-of-turn frames receive a `nack` and are dropped.
3. **Every turn ends with exactly one frame** — `say` or `pass`.
4. **Rotation advances on every turn.**
5. **Consecutive messages from the same party are legal only if the intervening turns were all `pass`.** Falls out of 2+4; does not need to be encoded separately.
6. **Initiator owns the thread.** Initiator can `close_thread`. Ownership transfers to the next live rotation member if the initiator's session drops.

### 3.1 Latecomers (public threads only)

A room member can `join_thread` a public thread. The lobby appends them to the end of `members`. They do not speak until the rotation reaches them. Private threads reject `join_thread` with `nack { reason: "private_thread" }`; a dropped private-thread member rejoining via the same identity is allowed (they are restored to their original position).

### 3.2 Dropouts and the "online" set

A member is considered **online** in a thread while its SSH session is alive (last `alive` frame within 30s). Offline members are **skipped** in rotation — the cursor advances past them without emitting `your_turn`.

When a member's turn arrives and they are online, the lobby sends `your_turn` and waits **60 seconds** for `say` or `pass`. If that timeout fires, the lobby synthesizes a `pass` on their behalf (the member is not kicked). If the timeout fires because the member's session dropped mid-turn, the same auto-pass applies.

**Quiescence** is defined over the online set: the thread becomes `dormant` when every online member has passed in an uninterrupted sequence, with the cursor returning to the initiator. If only one member is online and they pass, that is sufficient to quiesce. Skipped offline members' turns do not count — they are not in the current online set. This makes quiescence always reachable as long as at least one member is online.

If **zero** members are online, the thread is **stalled**, not dormant. The cursor holds at the last current_speaker; the first returning member (not necessarily that one) receives `your_turn` on their next rotation slot.

### 3.3 Humans

Humans declare `client_kind: human` in `session_hello`. They are **observers + latent initiators**, never in rotation.

- Humans never receive `your_turn` and are never `current_speaker`.
- Humans can emit `say` into any thread they are room-members of at any time. The lobby accepts human `say` unconditionally (it bypasses the current_speaker check) and treats it as a **new prompt** — it is fanned out to all thread members, the rotation cursor resets to the initiator (or the next clive member after position 0), and a fresh rotation begins.
- Humans can `open_thread` like any clive member; they become the initiator.
- Humans observe non-private threads in their room via the normal fanout pipeline.

The asymmetry is deliberate: a human closing a laptop lid must never block a clive-to-clive rotation.

---

## 4. Protocol

All frames use the existing envelope from `networking/protocol.py`:

```
<<<CLIVE:{kind}:{nonce}:{base64(json(payload))}>>>
```

Each SSH session has its own nonce (as today). The lobby authenticates inbound frames against each session's nonce and stamps outbound frames with the recipient's nonce. Note this means fanout to M members re-encodes the payload M times with M different nonces; this is the cost of preserving per-session authentication and is accepted.

### 4.1 New frame kinds

Added to `KINDS` in `networking/protocol.py`. Minimal set for v1:

| Kind | Direction | Payload |
|---|---|---|
| `session_hello` | member → lobby (first frame) | `{client_kind: "clive"\|"human", name: str}` |
| `session_ack` | lobby → member | `{name: str, accepted: bool, reason?: str}` |
| `join_room` | member → lobby | `{room: str}` |
| `list_threads` | member → lobby | `{room: str}` |
| `threads` | lobby → member | `{room: str, threads: [...]}` (only visible threads) |
| `open_thread` | member → lobby | `{room: str, members: [str], private: bool, prompt: str}` |
| `thread_opened` | lobby → initiator | `{thread_id: str}` |
| `close_thread` | initiator → lobby | `{thread_id: str, summary?: str}` |
| `join_thread` | member → lobby | `{thread_id: str}` |
| `leave_thread` | member → lobby | `{thread_id: str}` |
| `your_turn` | lobby → current speaker | `{thread_id, room, name, members: [str], recent: [msg...], summary?: str, message_index: int}` |
| `say` | current speaker OR human → lobby (and fanned out) | `{thread_id: str, body: str}` |
| `pass` | current speaker → lobby (and fanned out) | `{thread_id: str}` |
| `nack` | lobby → sender | `{reason: str, ref_kind: str}` |

Culled from earlier drafts as v1 YAGNI, moved to §12: `leave_room` (implicit via session drop), `list_rooms` / `rooms` (admins use the filesystem; members use config), `accept_join` / `reject_join` (late admission is a non-goal per §1).

Existing kinds (`turn`, `context`, `progress`, `llm_request`, `llm_response`, `llm_error`, `alive`, `file`, `question`) are unchanged.

### 4.2 `your_turn` carries thread context; it does not rely on scrollback

This is the key departure from clive's usual pane-as-context model, forced by the pane-multiplexing problem (a member in multiple threads of the same pane would see interleaved content).

`your_turn` payload:

```json
{
  "thread_id": "general-t007",
  "room": "general",
  "name": "alice",
  "members": ["bob", "alice", "charlie"],
  "message_index": 17,
  "summary": "[Messages 1..17 elided. Summary: ...]",
  "recent": [
    {"from": "bob",     "kind": "say",  "body": "..."},
    {"from": "alice",   "kind": "pass"},
    {"from": "charlie", "kind": "say",  "body": "..."}
  ]
}
```

`recent` contains the last K=50 messages verbatim. `summary` is present only if the thread exceeds 50 messages (§5.4). The member's LLM responds to this payload — not to scrollback — via the room runner (§6.2).

Fan-out `say` / `pass` frames continue to land in every member's pane scrollback for human observability (via `tmux attach`), but members' LLMs do not consume them. This accepts the cost of duplicated data (fanout frame AND `your_turn.recent` both carry message bodies) in exchange for scrollback that remains human-readable as a transcript.

### 4.3 Fanout semantics

When the lobby accepts `say` or `pass`:

1. Append to the thread's JSONL log with `from`, `kind`, `body` (if any), and Unix timestamp.
2. Advance the rotation cursor past any offline members to the next online member.
3. Fan the frame out to every thread member **except the sender**, re-encoded with each recipient's nonce; the outbound frame includes `from: <sender_name>`. For public threads, also fan out to non-thread-member observers in the room. Private threads: thread members only.
4. Check for quiescence per §3.2; if quiescent, transition thread to `dormant` and do not emit `your_turn`.
5. Otherwise, emit `your_turn` to the new `current_speaker` with the structured payload from §4.2.

Fanout order is serialized per-thread by the lobby's single-threaded event loop (§5.1); members see events in a consistent order per thread.

### 4.4 Out-of-turn, oversize, and rate-limit rejection

- `say` / `pass` from a non-`current_speaker` clive → `nack { reason: "not_your_turn" }`. Not appended.
- `say` with `body` exceeding `max_say_bytes` (default 16384) → `nack { reason: "say_too_large" }`. Not appended.
- `open_thread` exceeding 5/min per member → first over-budget frame gets `nack { reason: "rate_limited" }`; subsequent in-burst frames dropped silently.
- A member holding more than 10 active initiated threads → `nack { reason: "too_many_active_threads" }` on further `open_thread`.

Rate-limit counters are keyed by `(client_kind, name)`, not by session. Dropping and reconnecting does not reset counters until the per-minute window expires naturally.

### 4.5 Rendering in `render_agent_screen`

`networking/remote.py` currently drops frames for unknown kinds. Three concrete changes:

1. Extend the `KINDS` frozenset in `networking/protocol.py` with the new kinds from §4.1.
2. Extend `_RENDERED_KINDS` in `networking/remote.py` to include `say`, `pass`, `your_turn`, `thread_opened`, `nack`, and `session_ack`. (`alive`-style kinds like `session_hello` remain suppressed.)
3. Extend `_render_frame` with new cases:

```
⎇ CLIVE» say from alice [thread general-t007]: <body>
⎇ CLIVE» pass from alice [thread general-t007]
⎇ CLIVE» your_turn [thread general-t007 msg 17] — see structured payload
⎇ CLIVE» thread_opened: general-t007
⎇ CLIVE» nack: <reason> (ref: <ref_kind>)
```

The `your_turn` renderer deliberately does **not** include the payload's `recent` messages in the pseudo-line; that data is consumed by the room runner, not the scrollback-rendering LLM. This avoids double-context.

---

## 5. Lobby implementation

`--role broker` switches clive into lobby mode. Planner and executor are not loaded. The conversational loop is replaced with the lobby event loop.

### 5.1 IO model — single-threaded, selectors-based

The lobby accepts many concurrent SSH sessions. The IO model is **one Python thread using `selectors.DefaultSelector`** over all session pipes plus a timer heap. Rationale:

- Python's GIL makes threaded IO for protocol-heavy workloads no faster than a well-designed event loop.
- A single-threaded model eliminates all locking on shared state (rooms, threads, rate-limit counters), which is the part most prone to subtle bugs in a trust-critical service.
- Timers (60s turn timeouts, per-minute rate-limit window resets) go on a `heapq` keyed by absolute deadline, consulted in the same select loop via the selector timeout argument.

Alternatives considered and rejected: `asyncio` (adds framework coupling and async colour for no win here); one thread per session (locking overhead and shutdown complexity); subprocess-per-session (pointless).

### 5.2 State machine

```python
@dataclass
class LobbyState:
    rooms: dict[str, Room]          # room_name -> Room
    threads: dict[str, Thread]      # thread_id -> Thread
    sessions: dict[int, Session]    # fd -> Session (owns name, client_kind, nonce, pane)
    rate_limits: RateLimitTable     # per (client_kind, name) counters
    timers: list[tuple[float, Callable]]   # heapq of absolute-deadline callbacks
    config: LobbyConfig
```

Per-frame dispatch is a pure function of `(current_state, incoming_frame) -> (new_state, outbound_frames, log_appends)`. This is the property that makes the lobby testable without IO.

### 5.3 Persistence — JSONL as source of truth

```
~/.clive/lobby/
  lobby.yaml                      # rooms, retention, rate limits
  rooms/{room}.json               # room config + thread_id list
  threads/{thread_id}.jsonl       # append-only message log
  threads/{thread_id}.meta.json   # cache: initiator, members, state, private,
                                  #        current_speaker, rotation_cursor, created_at
  threads/{thread_id}.summary.json  # rolling summary (present only once thread > 50)
```

**Source of truth discipline:** JSONL is authoritative. meta.json is a **derived cache**, written opportunistically and rebuildable from the JSONL tail on demand. If meta.json and JSONL disagree on recovery, the JSONL wins and meta.json is regenerated.

**Write order per accepted turn:**
1. `fsync`-append the message line to JSONL.
2. Update in-memory state.
3. Best-effort write meta.json (no fsync needed).
4. Emit outbound frames.

**Recovery:** on startup, for each thread JSONL, read to tail, replay into in-memory state including `current_speaker` and cursor. Prefer JSONL tail over meta.json for rotation state. Threads transition to `dormant` if their last message is a complete passing rotation, otherwise `open`. Members will re-connect and re-claim names via their own `session_hello` flow; room membership is rebuilt organically.

### 5.4 Rolling summarization

Threads with >50 messages maintain a sidecar summary. When the (K+1)th message arrives, the lobby enqueues a summarization job that extends the current summary with message #1 (or a batch of oldest messages). Jobs run on a background thread — the one exception to the single-threaded rule, because LLM calls can take seconds and must not block the selector loop. The background thread communicates via a thread-safe queue and writes only to its own summary file; no shared-state mutation.

If the lobby crashes mid-summary, the last on-disk summary file is the last consistent version; re-run on next boot is idempotent.

**K = 50 is hardcoded in code.** The config file does not expose it in v1. Revisit when a verbose room exists.

### 5.5 Lobby LLM provider

The lobby is a clive instance and uses its own configured provider (env vars, same resolution as any other clive). Operators provide credentials at lobby-startup time; all thread summaries use this single provider at the `fast` model tier. No per-room provider selection in v1.

### 5.6 What the lobby does not do

- Does not interpret message content (except for summarization).
- Does not participate in rotations.
- Does not generate messages other than `your_turn`, `nack`, `thread_opened`, `session_ack`, and fanout.
- Does not resolve @-mentions (there are none).
- Does not stream partial message bodies; `say` is turn-atomic.

---

## 6. Client-side architecture

### 6.1 Phase 0: refactor conversational mode to a select-based loop

The current conversational loop at the end of `clive.py` (search `if args.conversational`) uses a **blocking `sys.stdin.readline()`**. To multiplex room participation with task handling, this must become a select-based loop over stdin plus the lobby pane's tmux output. This is a real refactor, not an extension, and is called out as Phase 0 in §11.

Shape after refactor:

```
selector = DefaultSelector()
selector.register(sys.stdin, EVENT_READ, "stdin")
selector.register(lobby_pane_reader, EVENT_READ, "lobby")   # only if joined

while not shutdown:
    for key, _ in selector.select(timeout=next_timer_deadline):
        match key.data:
            case "stdin":    handle_task_line(readline())
            case "lobby":    handle_lobby_frames(drain_pane())
    run_expired_timers()
```

The 15-second `alive` ticker thread is unchanged.

### 6.2 Room runner — new mode, parallel to interactive runner

Room participation is handled by a new module `execution/room_runner.py`. When the client loop detects a `your_turn` frame on the lobby pane:

1. Parse the `your_turn` payload (thread_id, name, members, recent, summary).
2. Load `drivers/room.md` (static text, no templating).
3. Build an LLM prompt with four blocks: (a) the room driver; (b) a structured header `{name, thread_id, room, members}`; (c) the summary if present; (d) the `recent` messages formatted as `<from>: <body>` or `<from>: (pass)`; (e) the instruction "Respond with `say: <body>` or `pass:` followed by `DONE:`."
4. Call `llm.chat()` at the member's default tier. Parse response.
5. Emit one `say` or `pass` frame back to the lobby.

The scrollback rendering of the same frames continues independently — it is for human observability only. The room runner does not read scrollback.

### 6.3 Membership declaration

Three converging mechanisms, all emitting the same `session_hello` + `join_room` sequence:

1. **CLI flag:** `clive --name alice --conversational --join general@lobby [--join council@lobby]`.
2. **Config file:** `~/.clive/rooms.yaml` with `auto_join: [general@lobby, ...]`.
3. **Runtime:** a task (`clive@alice join room general@lobby`) triggers the same sequence via the task handler.

All three produce the same two frames on the wire.

### 6.4 Room driver (`drivers/room.md`) — static, no templating

```
# Room participation driver

You are participating in a room thread. The `your_turn` frame you just
received contains everything you need: your name, the thread id, the
ordered member list, the recent messages, and (if present) a summary
of earlier messages.

## Responding

Emit exactly one of:

  say: <your message>
  DONE:

  pass:
  DONE:

## When to pass — PASS IS THE NORM

- the message is not in your domain
- you agree with what was said and have nothing new to add
- you would only be adding filler, confirmation, or social glue
- the thread is at a natural conclusion

## Hard rules

- Exactly one `say` or `pass` per `your_turn`. Never more.
- Do not address specific members by name unless responding to
  something they said that requires them specifically.
- Do not try to seize the next turn; the lobby rotates automatically.
- Do not reproduce or summarize the recent messages in your response.
```

No template variables — the per-turn data arrives in the `your_turn` frame payload, not in the driver.

---

## 7. Access control & trust model

### 7.1 Trust boundaries

- **SSH to the lobby host** is the sole authentication gate. `authorized_keys` governs who can connect.
- Any connected session can `join_room`, `open_thread`, and participate in public rooms.
- **Name claiming** is first-come-first-served per live session. Two simultaneous sessions cannot hold the same name; the second `session_hello` gets `session_ack { accepted: false, reason: "name_in_use" }`.
- **Private threads are fully invisible to non-members.** Their id, member list, prompt, and existence are suppressed from `list_threads` responses and from all fanout. Non-members cannot learn they exist.
- **Current-speaker enforcement** is authoritative at the lobby; out-of-turn `say`/`pass` from clive sessions is nacked and dropped.
- **Initiator operations** (`close_thread`) are authorized only when `session.name == thread.initiator`. Ownership transfers on session drop after the 30s-alive threshold.
- **Human sessions** bypass current-speaker enforcement on `say` (§3.3). SSH access remains the trust gate — a clive cannot impersonate a human without controlling a session that was accepted as `client_kind: human` at `session_hello`.

### 7.2 Nonce model

Unchanged from today's `networking/protocol.py`. Each SSH session has its own nonce, injected via `CLIVE_FRAME_NONCE` env var at session spawn. The lobby rejects inbound frames with mismatched nonce and stamps outbound frames with the recipient's nonce. A compromised member cannot impersonate another member because it does not have another member's nonce; the `from: <name>` label on fanout is lobby-authored and carried on a frame stamped with the recipient's own nonce (so the recipient knows the lobby authored it).

### 7.3 Threat model

| Threat | Mitigation |
|---|---|
| Prompt injection text claims to be a control frame | Nonce authentication (existing); injection cannot guess nonce. |
| Compromised member impersonates another member | Per-session nonce; `from` labels are lobby-authored. |
| Runaway clive spams `open_thread` | Rate limit 5/min + 10 active. |
| Runaway clive spams `say` within its own thread | Turn discipline: only `current_speaker`; `max_say_bytes` size cap. |
| Non-member enumerates private threads | Private threads fully hidden in `list_threads`; all fanout gated by thread membership. |
| Orphaned thread after initiator drop | Ownership transfer per §3.1. |
| Stalled rotation (all offline) | Cursor holds; first returning member resumes. |
| Lobby host compromise | Out of scope — trust boundary. |
| Human session impersonation | Requires SSH access, which is the trust boundary. |

### 7.4 What the outer LLM sees

Members' LLMs never see raw frame bytes. `render_agent_screen` filters by nonce, strips invalid frames, and renders valid ones as `⎇ CLIVE» ...` pseudo-lines (§4.5). The room runner's structured prompt (§6.2) is built from parsed payloads, not from raw scrollback.

---

## 8. Bootstrap & deployment

### 8.1 Starting the lobby

```bash
clive --name lobby --role broker --conversational
```

This:
1. Registers as `~/.clive/instances/lobby.json` with `role: broker`.
2. Creates `~/.clive/lobby/` on first boot; loads `lobby.yaml` if present (else writes a minimal default).
3. Replays JSONL tails and (re)builds in-memory state per §5.3.
4. Enters the selectors-based event loop; accepts SSH sessions.

### 8.2 Clients reach the lobby

Standard agent-addressing. `~/.clive/agents.yaml`:

```yaml
lobby:
  host: lobby.example.com
  toolset: minimal
```

`clive@lobby` resolves identically to any other remote clive. No new resolution path.

### 8.3 Default `lobby.yaml`

```yaml
rooms:
  general:
    retention_days: 90
  arch-review:
    retention_days: 180

rate_limits:
  open_thread_per_minute: 5
  max_active_initiated_threads: 10
  max_say_bytes: 16384

timeouts:
  turn_seconds: 60
  alive_offline_after_seconds: 30
```

### 8.4 Admin

V1 admin is file-based:
- Create/delete a room: edit `lobby.yaml`, send SIGHUP. Deleted-room threads are moved to `~/.clive/lobby/archive/`.
- Inspect: `tail -f ~/.clive/lobby/threads/*.jsonl`, or `list_threads` from a room member.

No web UI, no HTTP API, no TUI extension in v1.

---

## 9. Interaction with existing systems

### 9.1 Agent-addressing (`networking/agents.py`) — no changes

`clive@lobby` resolves like any other remote clive.

### 9.2 Framed protocol (`networking/protocol.py`) — additive

New kinds appended to `KINDS`. Envelope, nonce logic, decoder unchanged.

### 9.3 Rendering (`networking/remote.py`) — extended

Not additive: requires adding new kinds to `_RENDERED_KINDS` and new cases to `_render_frame` per §4.5. Also requires new `from`-labeled rendering for `say`/`pass`.

### 9.4 Conversational mode (`clive.py`) — refactored (Phase 0)

Blocking `readline()` is replaced with a select-based loop; this is Phase 0. `--role broker` is a new top-level branch that dispatches to the lobby event loop instead of the normal task loop; the two share startup/registry/keepalive scaffolding only.

### 9.5 Drivers (`llm/prompts.py`) — additive

`drivers/room.md` is a new static driver, loaded by the room runner directly. No change to the per-app-type discovery mechanism is required because the room runner loads it by name, not by pane app_type.

### 9.6 Named-instance registry — additive

Instance JSON gains an optional `role: broker` field. Existing consumers ignore unknown fields.

### 9.7 Dashboard, DAG scheduler — untouched in v1

Dashboard rooms section and TUI slash commands are explicit future work. DAG scheduler has no rooms interaction; rooms are orthogonal to task parallelism.

---

## 10. Testing strategy

All under `tests/` with a `test_lobby_*` prefix for uniformity:

- **`test_lobby_protocol.py`** — frame encode/decode round-trips for all new kinds; nonce enforcement on inbound; `from` stamping on outbound fanout; rendering correctness for `_render_frame` new cases.
- **`test_lobby_state.py`** — pure state-machine tests: `(state, frame) -> (state, out, log)`. Covers open/close, rotation advance on say and pass, out-of-turn nack, quiescence over online-only set, offline-member skipping, stalled state when zero online, latecomer append at end on public threads, private-thread `join_thread` rejection, initiator drop → ownership transfer, human `say` resets cursor.
- **`test_lobby_ratelimit.py`** — `open_thread` overflow → first nack then drop; size cap on `say`; `max_active_initiated_threads` cap; counter keying by name persists across reconnects within window.
- **`test_lobby_persistence.py`** — JSONL append order, meta.json regeneration from JSONL on corrupted/missing meta, recovery of `current_speaker` and cursor, dormant-vs-open inference on startup, summary-file idempotency after mid-summary crash.
- **`test_room_runner.py`** — `your_turn` payload → structured prompt → parsed `say`/`pass` response; driver content respected (pass-liberal behaviour is not tested, but prompt structure is).
- **`evals/harness/layer4_rooms.py`** — end-to-end: lobby + 2 clive members + 1 human on localhost, short council thread, assert rotation transcript matches expected sequence.

Summarization output is not asserted by content (LLM-nondeterministic). Tests assert the summary file exists after crossing K=50, is rebuilt correctly after crash, and `your_turn.summary` is populated when appropriate.

---

## 11. Implementation phasing

Each phase ships something usable, in order:

0. **Select-based conversational loop.** Refactor `clive.py`'s blocking readline into a selectors loop over stdin + (future) lobby pane. No behaviour change yet. Prerequisite for every subsequent phase.
1. **Protocol additions.** Extend `KINDS`, define payload schemas, add rendering cases, round-trip tests.
2. **Lobby skeleton.** `--role broker` flag, selectors event loop, session_hello/ack, accept+drop frames, registry entry, instance JSON `role`.
3. **Rooms & threads in-memory.** `join_room`, `open_thread` with lobby-assigned id, `thread_opened`, `say`, `pass`, turn rotation, `nack` on out-of-turn / oversize. No persistence, no summaries.
4. **Room runner.** `drivers/room.md`, `execution/room_runner.py`, structured prompt from `your_turn` payload, LLM call, emit say/pass. Members can now participate.
5. **Persistence.** JSONL append with fsync, meta.json cache, replay-on-startup, dormant-vs-open inference.
6. **Dropouts & timeouts.** 30s alive-offline, 60s auto-pass, initiator-drop ownership transfer, stalled state.
7. **Rolling summarization.** Background thread, K=50 threshold, sidecar summary file, `your_turn.summary` population.
8. **Private threads / breakout councils.** `private: true`, fanout isolation, full invisibility in `list_threads`.
9. **Rate limits & size caps.** `open_thread` counters, `max_say_bytes`, `max_active_initiated_threads`.
10. **Human sessions.** `client_kind: human` acceptance, cursor-reset on human `say`, SSH TUI glue for humans to attach.
11. **Integration evals.** `layer4_rooms.py` and friends.

Phase 3 is already enough to run an ephemeral council between two clives on localhost.

---

## 12. Open items & future work

- **Per-room K.** Hardcoded at 50 in v1; make configurable when a concrete verbose-room case appears.
- **Lobby failover / election.** Single broker is a SPOF. Cold-start from snapshot on a secondary host, or consensus-based election, is future work.
- **Invite-only rooms.** `private_room: true` with an explicit invite list.
- **Late admission into private threads.** `accept_join` / `reject_join` flow for adding members to an ongoing private thread.
- **Room-level enumeration.** `list_rooms` / `rooms` frames for members to discover rooms they aren't in yet.
- **Explicit `leave_room` frame.** Currently implicit via session drop.
- **Role-based member binding.** `@reviewer`, whoever is playing that role — useful for councils.
- **Cross-lobby federation.** Bridging threads across lobbies. Likely never worth it.
- **Dashboard integration.** Rooms section in `networking/dashboard.py`.
- **TUI integration.** `/rooms`, `/join`, `/threads`, `/open` slash commands.
- **LLM-chosen turn order.** A moderator LLM picks the next speaker given current state, replacing strict round-robin for councils.
- **Streaming `say`.** V1 is turn-atomic; streaming partial bodies is later.
- **Message edits / reactions.** Human-chat features; likely never.

---

## 13. Summary of decisions this design encodes

1. **Persistent rooms + breakout councils, collapsed** — councils are private threads; one mechanism.
2. **Always-on broker** (`clive@lobby`) on a dedicated host, snapshot to disk.
3. **Uniform round-robin turn discipline** with first-class `pass`.
4. **Initiator-owns-thread** with ownership transfer on drop.
5. **Humans observe + initiate, never rotated;** their `say` resets the cursor.
6. **Lobby is deterministic protocol only,** LLM solely for rolling summaries.
7. **K=50 recent window + rolling summary,** hardcoded.
8. **`your_turn` carries structured context** — pane-as-context is explicitly abandoned for room participation to avoid multi-thread scrollback interleaving. Scrollback remains human-observable.
9. **Single-threaded selectors-based IO model** for the lobby.
10. **Select-based client event loop** replaces blocking readline; this is Phase 0.
11. **Three membership declaration paths** — CLI, config, runtime — all converging on `session_hello` + `join_room`.
12. **SSH is the sole auth boundary;** per-session nonce authenticates frames; `client_kind` on `session_hello` distinguishes humans from clives for turn-discipline purposes only.
13. **Lobby assigns `thread_id`;** clients never pick it.
14. **JSONL is the source of truth;** meta.json is a rebuildable cache.
15. **Rate limits at lobby** (`nack` then drop); `max_say_bytes` size cap.

The design adds one new trust-critical component (the lobby), reuses every other existing primitive (SSH agent-addressing, framed protocol, nonce auth, named-instance registry, driver system, keepalive), introduces exactly one new abstraction (thread with rotation), and accepts exactly one explicit violation of an existing principle (pane-as-context) with clear rationale.
