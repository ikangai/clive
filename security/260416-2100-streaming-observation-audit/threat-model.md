# Threat Model — Streaming Observation Branch

## Scope

Audit of commits `bcc5b4f..75873aa` on `feature/streaming-observation` (24 commits, ~5700 LOC added). The branch introduces a FIFO-based byte-stream observation pipeline (`ByteClassifier`, `PaneStream`, `PaneLoop`) and a version-stamped speculation scheduler (`SpeculationScheduler`) wired into `interactive_runner`.

## Clive's trust model

Clive is a **local CLI tool** driving tmux panes on behalf of an LLM. The relevant adversaries:

1. **Local unprivileged user** on the same host — can read world-readable files, list `/tmp/`.
2. **Malicious pane content** — an LLM-issued `curl` or the user visiting a crafted page returns adversarial bytes into the pane. The observation pipeline must classify these bytes without misbehaving.
3. **Compromised or prompt-injected LLM** — a cloud LLM can be influenced to emit harmful commands. Speculation accelerates the blast radius here (commands execute earlier, on staler context).
4. **Another clive instance on the same host** — multiple clive invocations may collide on `/tmp/clive/` state.

Out of scope: root-level attacks, kernel bugs, tmux itself, libtmux, Python standard library.

## Assets

| Asset | Sensitivity | Storage |
|-------|-------------|---------|
| Pane bytes (stdout + stderr of commands) | **High** — may contain passwords, tokens, API keys, file contents, command history | FIFO at `/tmp/clive/{session_id}/pipes/{pane_name}.fifo`; tmux buffer; capture-pane output |
| LLM API keys | Critical | Env vars; never in this branch's code paths |
| Subtask IDs and markers | Low | In wrap_command output; visible to anyone reading the pane |
| `messages_snapshot` (speculation) | Medium — contains full conversation context | In-process; never written to disk |
| Scheduler metrics counters | Low — operational only | In-process; logged at INFO |

## Trust boundaries

```
┌──────────────────────┐         ┌──────────────────────┐
│ pane subprocess      │         │ clive runner thread  │
│ (shell + children)   │◄───1────┤ (send_keys)          │
│                      │────2────►│                      │
└──────────────────────┘         └──────────────────────┘
         │                                  ▲
         │ 3 (pipe-pane via shell -c)       │
         ▼                                  │ 5 (subscribe)
┌──────────────────────┐   4    ┌──────────┴────────────┐
│ FIFO in /tmp         │───────►│ PaneStream reader     │
│ (filesystem)         │        │ (pane loop thread)    │
└──────────────────────┘        └───────────────────────┘

Boundaries:
  1  tmux command injection surface (commands the LLM chose to run)
  2  Observed bytes (adversarial input to byte classifier)
  3  Shell interpretation of `cat > {fifo_path}` — metachar injection surface
  4  Filesystem boundary — permissions, symlinks, shared /tmp
  5  Thread-crossing + asyncio queue semantics
```

## STRIDE matrix (branch-specific)

| Threat | Applicable to | Example |
|--------|---------------|---------|
| **S**poofing | FIFO writes (if world-writable) | Local user writes `EXIT:0 ___DONE_x` to FIFO, triggers false completion |
| **T**ampering | `messages_snapshot` prefix check | LLM speculates on stale context; runner uses reply despite missing turn data |
| **R**epudiation | Speculation accept/discard | No audit log of which reply came from where (only counters) |
| **I**nformation disclosure | FIFO read perms | Other users on host read pane bytes including passwords |
| **D**enial of service | Circuit breaker; subscriber queues | Pane producing huge bursts backpressures tmux pane |
| **E**levation of privilege | Speculation semantic hole | LLM acts on staler context → earlier/wrong command issuance |

## Attack surface summary

| Entry point | File:line | Trust boundary |
|-------------|-----------|----------------|
| `_maybe_attach_stream` mkfifo + pipe-pane | `session.py:193-209` | Filesystem + shell command |
| `PaneStream._read_loop` raw byte intake | `fifo_stream.py:61-87` | Adversarial pane output |
| `ByteClassifier.feed` regex scan | `byte_classifier.py:54-79` | Adversarial pane output (ReDoS vector) |
| `SpeculationScheduler.try_consume` | `speculative.py:117-146` | Stale-context LLM replies |
| `_spec_watch` messages_snapshot capture | `interactive_runner.py:173-184` | Cross-thread shared state |
| `latency_bench.run_scenario_*` subprocess | `latency_bench.py` | Process boundary (bench only) |
