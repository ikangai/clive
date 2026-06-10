# Hypothesis Queue — for optional `--chain debug` follow-up

| Rank | ID | Hypothesis | Confidence | Location | Source |
|------|----|-----------|-----------|----------|--------|
| 1 | H-01 | `ScreenClassifier.classify()` is never invoked when `exit_code != 0` in production runners; Branch 3 (`observation.py:92-100`) is dead code from the runners' perspective | HIGH | `interactive_runner.py:351`, `toolcall_runner.py:242-248` | swarm 5/5 |
| 2 | H-02 | `cost_tokens` field is hard-coded to 0 in both `run_scenario_baseline` and `run_scenario_phase1`; the proposed merge-gate metric is meaningless on the existing bench | HIGH | `latency_bench.py:145, 249`; `metrics.py:14, 47` | swarm 5/5 |
| 3 | H-03 | `shlex.split("git diff --quiet")[0]` returns `"git"`, not `"git diff --quiet"` — the proposal's first-token matcher fails on its own headline example | HIGH | `observation.py:92` (proposed); `candidate_A.md:13` | swarm 5/5 |
| 4 | H-04 | Compound commands (`grep foo; rm -rf /`) survive `shlex.split` with first token `grep` — first-token allowlist creates a covert-channel primitive | HIGH | `observation.py:92` (proposed) | swarm 5/5 |
| 5 | H-05 | `git diff --quiet` run outside a repo exits 128 ("fatal: not a git repository"); proposal classifies as informational, hiding a real failure | HIGH | `observation.py:92-100` (proposed) | swarm 5/5 |
| 6 | H-06 | `last_command` is not in scope at `toolcall_runner.py:246`; the proposal requires a result-dict schema change | HIGH | `toolcall_runner.py:53-81, 234-248` | swarm 5/5 |
| 7 | H-07 | When the sandbox wrapper exits non-zero for policy reasons (not the inner command), the recorded `exit_code` is the wrapper's; proposal classifies as inner-command informational | HIGH | `runtime.py:77-86`; `interactive_runner.py:92`; `toolcall_runner.py:66` | swarm 5/5 |
| 8 | H-08 | `shlex.split` raises `ValueError`/`IndexError` on unbalanced quotes / empty / trailing backslash; LLM-emitted commands routinely mis-quote | HIGH | `observation.py:56` (proposed) | swarm 5/5 |
| 9 | H-09 | `missed_rate` measures L2 byte-event detection only; an INFORMATIONAL_NONZERO that silences every real failure would not change `missed_rate` | HIGH | `metrics.py:46`; `latency_bench.py:131, 137, 225-235` | swarm 5/5 |
| 10 | H-10 | The 30% threshold was retired from `phase1-report.md:40, 47-50` for latency; reusing it for `cost_tokens` is dimensionally incompatible | HIGH | `candidate_A.md:22`; `phase1-report.md:40, 47-50` | swarm 5/5 |
| 11 | H-11 (minority — preserved) | Branch 6 UNKNOWN catch-all may be the dominant `exit==0` escalation drain; proposal targets a sub-dominant term | MEDIUM | `observation.py:123-130` | swarm 1/5 (DA), PE endorsed as "most underrated" |
