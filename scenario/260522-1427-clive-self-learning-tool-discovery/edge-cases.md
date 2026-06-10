# Edge Cases & Failure Modes — Clive `--explore` (gh#41)

Severity-ranked subset of scenarios.md for triage. Each row references its full Given/When/Then in scenarios.md.

## CRITICAL — fix before next release

| ID | Title | Mechanism | Mitigation |
|---|---|---|---|
| #4 | Prompt injection via `--help` text | Malicious tool's help output steers synthesizer LLM | Content filter on generated driver + structured DSL for PRIMARY TOOLS |
| #5 | Path traversal in tool name | `_SAFE_NAME` only checked at write time, after exploration runs | Validate at top of `handle_explore` + `explore_tool` |
| #16 | Command injection via tool name in `build_exploration_goal` | Interpolation of `tool_name` into LLM prompt steers it | Same as #5 (early validation) + sanitize prompt interpolation |
| #20 | TOCTOU write race between two `--explore` runs | `exists()` check is not atomic vs `open(...,"w")` | `os.open(O_CREAT|O_EXCL|O_WRONLY)` + per-tool `flock` |
| #30 | Section regex passes; malicious payload smuggled inside PRIMARY TOOLS body | Validator only checks markers, not content | Filter `curl|bash`, `eval`, etc.; structured DSL preferred |
| #43 | Base64-encoded payload in PRIMARY TOOLS bypasses naive content filter | Obfuscation evades regex scan | Structured DSL, not free-text shell |
| #48 | URL-encoded / hex-encoded payload survives filter | Same obfuscation class as #43 | Same as #43 |

## HIGH — fix in next sprint

| ID | Title | Why HIGH |
|---|---|---|
| #2  | Synthesizer omits PATTERNS section | Full exploration cost wasted; no caching of result |
| #6  | Two parallel `--explore` race on same tool | FileExistsError for the loser; LLM tokens spent for nothing |
| #7  | ANSI escape injection in `--help` output | Terminal hijack of operators reading audit logs |
| #10 | CREDENTIAL_TOOLS bypass via wrapper rename | Name-based deny list is trivially evaded |
| #12 | `--explore-overwrite` clobbers hand-written driver | Data loss; recoverable only via VCS |
| #13 | Probe stalls because `man` invokes PAGER (`less`) | No per-probe wall-clock timeout; user-visible hang |
| #14 | Ctrl-C mid-exploration leaks tmux session + `/tmp` dir | Unbounded resource leak across many interruptions |
| #17 | `tool_name="explore"` overwrites the meta-driver | Self-modification of the discovery system itself |
| #22 | PITFALLS section silently omitted | Validator doesn't enforce it; safety warnings drop silently |
| #23 | Frontmatter value injection (`agent_model: "fast; rm -rf /"`) | Unknown-value handling depends on downstream parser |
| #25 | `max_turns=0` API misuse → empty result → hallucinated driver | No minimum-probes guard |
| #26 | No contract test that `interactive_runner` emits `probe` events | Future refactor silently breaks the whole feature |
| #27 | Target tool prints `[AGENT_READY]` marker | PS1 control-plane signal is observable + spoofable from data plane |
| #33 | Social-engineered `--explore-overwrite` | Copy-paste attack replaces tuned hand-written drivers |
| #35 | Multiple section headings allow spoofing via duplicates | Validator counts ≥1, not exactly-1; ordering not enforced |
| #39 | Subcommand tools (`git`, `kubectl`, `cargo`) get useless top-level-only driver | Most production tools are subcommand-shaped |
| #41 | Honest synthesizer verbatim-quotes destructive example from help text | No content filter even for non-malicious inputs |
| #42 | Disk full during write → partial driver file | Non-atomic write; file lands as `---\nfront\n---\n` with no body |
| #45 | Case-collision FS (`RG` vs `rg`) | macOS APFS default is case-insensitive; silent overwrite |
| #46 | Tool name `foo.md` or `example.com` allowed by `_SAFE_NAME` | Confusing filenames, FS edge cases |
| #47 | User edits driver in vim while `--explore-overwrite` runs | Editor's later save clobbers fresh driver |
| #50 | No quarantine — auto-gen drivers used immediately by next pane | Multiplies every other abuse vector |

## MEDIUM — track for follow-up

| ID | Title |
|---|---|
| #3  | Tool emits empty `--help`; synthesizer hallucinates |
| #8  | tmux server unavailable; orphan `/tmp` dir |
| #9  | Tool name starts with hyphen; argparse + shell confusion |
| #11 | `drivers/` read-only; PermissionError uncaught |
| #15 | 50k-line `--help`; only first 12 lines reach synthesizer |
| #18 | Adversarial cost amplification capped by `max_tokens=1500` (regression-prone) |
| #19 | LLM 429 mid-synthesis; no retry; exploration result lost |
| #24 | env-var stripping with CREDENTIAL_TOOLS — edge cases lack test coverage |
| #28 | Target tool segfaults mid-probe; core dump leaks |
| #29 | Tool found via custom PATH; driver assumes default PATH |
| #31 | `drivers/` sprawl; no provenance metadata for pruning |
| #32 | Driver written months ago for tool whose flags have changed |
| #34 | Tool exits non-zero on `--help`; synthesizer biased against valid output |
| #36 | Container without tmux; opaque failure |
| #37 | tldr cache poisoning |
| #38 | Parallel planner DAG + `--explore` share tmux server |
| #40 | Re-explore existing auto-gen driver forces same flag as hand-written overwrite |
| #44 | Clock skew → wrong date in auto-gen header |
| #49 | Partial driver on disk — frontmatter ok, body truncated |

## LOW

| ID | Title |
|---|---|
| #44 | Auto-gen date wrong due to clock skew (cosmetic) |

## What-if expansion seeds (for future deeper passes)

- **What if** the synthesizer LLM is run on a third-party provider compromised after deployment? (cascade with #4, #30, #43)
- **What if** `drivers/` is mounted from a shared NFS volume? (concurrency-#6, #20, #47 amplify across hosts)
- **What if** `--explore` is invoked by a CI pipeline on every push? (#31 sprawl, #18 cost amplification, #50 quarantine become acute)
- **What if** a future PR adds `--explore --batch tool1 tool2 tool3 ...`? (Concurrency model implicit; #20 fan-out)
- **What if** the system loses power between `open()` and the final `write()`? (#42 + #49 combined; today's recovery is "manual `rm`")
