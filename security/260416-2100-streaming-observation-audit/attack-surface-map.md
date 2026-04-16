# Attack Surface Map — Streaming Observation Branch

## Entry points (attacker-influenceable byte paths)

### Shell-interpreted tmux commands

- `pane.cmd("pipe-pane", "-o", f"cat > {fifo_path}")` — `session.py:209`
  - tmux runs the third argument via `/bin/sh -c`.
  - `fifo_path = {base}/pipes/{pane_name}.fifo` where `base = session_dir or "/tmp/clive"`.
  - Injection surface: `pane_name` (from toolset) and `session_dir` (from caller).
  - Currently `pane_name` is one of fixed toolset values; `session_dir` is `/tmp/clive/{uuid_hex}`.
  - **Safe today, fragile**: any future toolset with a space/quote/semicolon in `name` → shell injection.

### Raw byte intake

- `PaneStream._read_loop` → `ByteClassifier.feed(chunk)` — `fifo_stream.py:61-76`, `byte_classifier.py:54-79`
  - Input source: whatever the pane writes (shell output, curl responses, file contents viewed).
  - Classifier runs 9 regex patterns on each chunk.
  - If an attacker can choose pane content, they control classifier output.
  - Risks: pattern collisions (intervention false positives), ReDoS (analyzed — not present), event spoofing via adversarial output.

### FIFO file itself

- `/tmp/clive/{session_id}/pipes/{pane_name}.fifo`
  - Default mode after `os.mkfifo(path)`: `0o666 & ~umask`. With typical macOS/Linux umask `0o022`, the FIFO ends up **world-readable (0o644)**.
  - Readers on the same host can intercept pane bytes.
  - Writers: if group/other has write bit (umask `0o002` or narrower), they can inject fake bytes.

## Data flows

```
1. Command → pane:
   wrap_command(cmd, subtask_id)
     → pane.send_keys(wrapped, enter=True)
     → bash runs: {cmd}; echo "EXIT:$? ___DONE_<id>_<nonce>___"

2. Pane bytes → classifier:
   tmux captures stdout/stderr
     → pipe-pane shell: cat > /tmp/clive/.../pipes/{name}.fifo
     → PaneStream reader: os.read(fd, 4096)
     → ByteClassifier.feed(chunk) → [ByteEvent(...)]
     → subscriber queues (fan-out)

3. Events → runner/scheduler:
   _spec_watch coroutine (on pane loop thread):
     await queue.get() → scheduler.fire(evt, list(messages))
   _send_agent_command (runner thread):
     pane_loop.submit(await_ready_events(...)).result(...)

4. Speculation:
   _spec_watch → scheduler.fire → chat_stream on run_in_executor
     → future.done() observable from runner thread
   Runner: scheduler.try_consume(current_messages)
     → if prefix matches: use reply; else: chat_stream fresh
```

## Abuse paths

### AP-1: Pane-bytes injection of fake completion

**Prerequisite:** attacker can write to the FIFO (umask `0o002` or laxer, or attacker controls pane content).
**Steps:**
1. Attacker writes `b"EXIT:0 ___DONE_foo_bar___"` to the FIFO (or emits via pane).
2. `ByteClassifier.cmd_end` fires with the marker.
3. `await_ready_events` returns `("<captured screen>", "marker")` — the runner thinks the current command completed with exit 0.
4. Runner proceeds to next turn based on stale screen content.

**Impact:** false completion, runner assumes success; if subsequent turn is destructive, damage amplifies.

### AP-2: FIFO exfiltration

**Prerequisite:** attacker has a shell on the same host; FIFO mode is world-readable (default).
**Steps:**
1. Attacker `cat /tmp/clive/*/pipes/*.fifo` in a loop.
2. When clive starts piping pane output, attacker receives bytes in real time.

**Impact:** passwords typed into sudo prompts, API tokens in file contents viewed, command history — leaked.

### AP-3: Speculation on stale context (semantic)

**Prerequisite:** `CLIVE_SPECULATE=1`.
**Steps:**
1. Runner issues command `rm -rf /dangerous`.
2. Command fails (exit 1) — runner will append `[EXIT:1]` notice in next turn.
3. Before that, `cmd_end` event fires; `_spec_watch` snapshots messages (without `[EXIT:1]`).
4. Speculation LLM call runs with snapshot missing `[EXIT:1]`.
5. Runner captures screen, appends diff. `try_consume` passes prefix check (snapshot is prefix of current).
6. Speculation's reply: "great, the rm succeeded; now let's cleanup the logs" — **based on missing exit failure**.
7. Runner issues the cleanup command thinking the rm worked.

**Impact:** action on wrong belief of state. Behind `CLIVE_SPECULATE=1` default-off; still a footgun.

### AP-4: /tmp squatting

**Prerequisite:** attacker has a shell on the same host; runs before clive.
**Steps:**
1. Attacker creates `/tmp/clive/pipes/shell.fifo` as a FIFO or symlink.
2. Clive starts. `os.makedirs(fifo_dir, exist_ok=True)` succeeds (attacker owns).
3. `os.path.exists()` returns True. `os.unlink()` tries to remove — fails because attacker owns the dir.
4. Exception caught by silent-fallback handler.
5. Clive falls back to polling.

**Impact:** DoS of streaming observation; not a breach. Fallback path preserves functionality.

### AP-5: Speculation storm (resource exhaustion)

**Prerequisite:** `CLIVE_SPECULATE=1`; attacker can cause rapid L2 triggers.
**Steps:**
1. Attacker writes many `EXIT:<n> ___DONE_..._...___` patterns to the FIFO.
2. `_spec_watch` fires `scheduler.fire()` repeatedly.
3. Rate limit (200ms) + circuit breaker (5 cancellations/60s) bound the impact.
4. But within 200ms of each fire, attacker has consumed an LLM call's cost.

**Impact:** cost inflation bounded by MIN_FIRE_INTERVAL + BREAKER_THRESHOLD; still real cost burn for attacker-triggered speculation.
