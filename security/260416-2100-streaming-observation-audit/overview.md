# Security Audit — Streaming Observation Branch

**Date:** 2026-04-16 21:00
**Scope:** `feature/streaming-observation` branch (24 commits, ~5700 LOC added)
**Focus:** new code in `src/clive/observation/`, `src/clive/execution/`, `src/clive/session/session.py`, `evals/observation/`
**Commit audited:** `75873aa` (HEAD of branch)
**Depth:** Deep audit (30+ vectors considered)
**Disposition:** Report + auto-fix Critical/High

## Summary

- **Total Findings:** 13 (0 Critical, 1 High, 3 Medium, 8 Low, 1 Info)
- **STRIDE Coverage:** 6/6 categories tested
- **OWASP Coverage:** 9/10 categories tested (A10 SSRF not applicable to this branch)
- **Confidence:** 7 Confirmed, 6 Likely/Possible/Info

## Headline

One real finding worth acting on before merge:

> **[HIGH] F-1 — FIFO is world-readable by default.** Pane bytes — including `sudo` password prompts, file contents, and any data the LLM pipes through `cat`/`env`/etc. — are intercept-able by any other local user on the host. One-line fix: pass `mode=0o600` to `os.mkfifo`.

Everything else is defense-in-depth or design-level discussion. Nothing is exploitable from outside the host; no RCE, no auth bypass.

## Top 3 findings

1. **[HIGH] [F-1 — FIFO world-readable permissions](./findings.md#high-f-1--fifo-created-with-default-umask-permissions-world-readable)** — `os.mkfifo(path)` without `mode=` inherits umask; on default `0o022` the FIFO is `0o644`. Other local users can read pane output in real time.
2. **[MEDIUM] [F-2 — Snapshot-prefix check accepts stale-context speculations](./findings.md#medium-f-2--snapshot-prefix-check-accepts-speculations-on-stale-context)** — behind `CLIVE_SPECULATE=1` (default off) but a known footgun. Logged and metered; semantic fix deferred per the Phase 2 disposition.
3. **[MEDIUM] [F-3 — `fifo_path` interpolated into `pipe-pane` shell command without quoting](./findings.md#medium-f-3--shell-interpretation-of-fifo-path-in-pipe-pane-command)** — currently unreachable with shipped toolsets; footgun for future toolset additions.

## Files in this report

- [Threat Model](./threat-model.md) — STRIDE matrix, assets, trust boundaries, attack surface summary
- [Attack Surface Map](./attack-surface-map.md) — entry points, data flows, abuse path walkthroughs
- [Findings](./findings.md) — all 13 findings with locations, code evidence, mitigations
- [Recommendations](./recommendations.md) — priority-ordered action items with exact fixes
- [Iteration Log](./security-audit-results.tsv) — one row per vector analyzed

## Disposition

Per the `/autoresearch:security` invocation (`Report + auto-fix Critical/High`):

- **F-1** will be auto-fixed in this audit run (one-line change to `os.mkfifo(..., mode=0o600)` in `session.py` + two sites in `latency_bench.py` for symmetry).
- F-2 through F-13 are reported only — no auto-fix. F-2, F-3, F-4 should be addressed in follow-up work. F-5 through F-13 are low-priority defense-in-depth.

## Validation

All findings are backed by code-reading or runtime verification. No speculative theoretical risks. "Confirmed" findings are reproducible with minimal setup; "Likely" findings have clear code paths but depend on specific conditions (umask, malicious pane content, attacker pre-staging); "Possible" findings are narrow races that require specific interleavings.

Nothing in this audit reports "might happen if" without a corresponding code path to point at.
