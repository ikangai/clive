# Summary — Clive Self-Learning Tool Discovery (gh#41) Scenario Exploration

**Seed:** `clive --explore <tool>` pipeline — bounded `--help`/`-h`/`man`/`tldr` probes against an unknown CLI in a fresh exploration pane, then LLM-synthesized `drivers/<tool>.md`.

**Configuration:** Domain = software + security; Format = test-scenarios (Given/When/Then); Iterations = 50 (deep).

**Composite metric:**
```
scenarios_generated  = 50 × 10                = 500
edge_cases_found     = ~18 × 15               = 270
dimensions_covered   = 12/12 × 30             = 30
unique_actors        = 7 × 5                  = 35
high_severity        = (6 critical + 23 high) × 3 = 87
─────────────────────────────────────────────
scenario_score = 922
```

## Severity heatmap

| Severity | Count | Notable IDs |
|----------|-------|-------------|
| CRITICAL | 6     | #4 (prompt injection via --help), #5 (path traversal in name), #16 (cmd injection via tool name), #20 (TOCTOU write race), #30 (smuggled payload in PRIMARY TOOLS), #43 (base64 evasion), #48 (URL-encoded evasion) |
| HIGH     | 23    | #2 lost result, #6 concurrent race, #7 ANSI injection, #10 CRED bypass via rename, #12 hand-written clobber, #13 PAGER hang, #14 KbI leak, #17 meta-driver collision, #22 PITFALLS not required, #23 frontmatter injection, #25 max_turns=0, #26 contract test gap, #27 PS1 spoof, #33 social-engineered overwrite, #35 section spoof via dup headings, #39 subcommand tools, #41 verbatim destructive example, #42 disk-full corrupt write, #45 case-collision FS, #46 dot-allowance, #47 editor race, #50 no quarantine |
| MEDIUM   | 19    | per-dimension distributed (see scenarios.md) |
| LOW      | 1     | #44 clock-skew header |
| —        | 1     | #1 baseline happy-path |

## Dimension coverage (12/12)

| Dimension       | Count | Hottest issue                                             |
|-----------------|-------|-----------------------------------------------------------|
| abuse           | 16+1  | Prompt/content injection chain (#4 → #30 → #43 → #48) is critical and unmitigated |
| edge_case       | 8     | Case-collision FS (#45), dot-allowance (#46), meta-driver collision (#17) — `_SAFE_NAME` too permissive |
| recovery        | 4     | Ctrl-C leak (#14), disk-full corruption (#42) — non-atomic write surface |
| integration     | 4     | Contract test gap (#26) on `on_event("probe")` is biggest regression vector |
| concurrent      | 3+1   | TOCTOU on driver write (#20) is silent corruption |
| state_transition| 3     | No quarantine (#50) multiplies every abuse vector |
| scale           | 3     | drivers/ sprawl (#31), 50k-line --help truncation (#15) |
| temporal        | 3     | PAGER hang (#13) is highest-impact |
| data_variation  | 2     | env-var stripping mostly OK; subcommand structure (#39) is a coverage gap |
| permission      | 1     | drivers/ ro (#11) — generic OSError handling |
| error_path      | 1     | Lost exploration on validation failure (#2) |
| happy_path      | 1     | Baseline #1 |

## Top 6 mitigations (highest leverage)

1. **Quarantine auto-gen drivers** (mitigates #4, #30, #41, #43, #48, #50) — write to `drivers/.unreviewed/` until reviewed; planner refuses unreviewed drivers unless explicit env flag. This single change blunts the entire prompt-injection chain.

2. **Structured DSL for `PRIMARY TOOLS`** (mitigates #30, #41, #43, #48) — instead of free-text shell, drivers use `command: foo` + `args: [...]`. Removes the surface where injection can smuggle shell pipelines.

3. **Validate `tool_name` at the top of `handle_explore` and `explore_tool`** (mitigates #5, #9, #16, #17, #45, #46) — tighten `_SAFE_NAME` to `^[a-z][a-z0-9_-]*$` (no dots, no uppercase) and check before exploration, not just at write. Refuse reserved names (`explore`, `shell`, …).

4. **Atomic, exclusive write** (mitigates #6, #20, #42, #47, #49) — `os.open(O_CREAT|O_EXCL|O_WRONLY)` for new drivers; write to `.tmp` + `os.replace` for refresh; mandatory `flock` for overwrite. Resolves every concurrency + corruption scenario in one change.

5. **Driver provenance + version metadata in frontmatter** (mitigates #12, #31, #32, #33, #40) — `provenance: hand-written|auto-explore`, `explored_at:`, `target_version:`. Enables safe-refresh vs force-overwrite split; supports stale detection.

6. **Per-probe wall-clock timeout + PAGER=cat in exploration pane env** (mitigates #13, #27, #28) — bounds the worst-case time per probe; defangs `man`/`less` traps and PS1-spoof attacks via behavioral detection on top of name-based lists.

## Recommended next steps

- **Chain into `/autoresearch:debug`** scoped to `src/clive/discovery/**` with symptoms = the 6 CRITICAL scenarios. Build a failing test per critical scenario (#4, #5, #16, #20, #30, #43, #48) before writing mitigations.
- **Chain into `/autoresearch:security`** with seed = scenarios #4, #16, #30, #43, #48 — they form an OWASP LLM01 + A03 (Injection) cluster worth a focused threat model.
- **Add contract test for `on_event("probe", ...)`** (#26) — single low-cost change with high regression-prevention payoff.
- **Defer until gh#39 lands**: scenarios #50 (quarantine) and #31 (provenance metadata) interact with the toolset auto-categorization design — coordinate the frontmatter schema.

## Files produced

- `scenarios.md` (1031 lines) — full Given/When/Then for all 50 situations with severity, recommended mitigations, and test pointers
- `scenario-results.tsv` — machine-readable iteration log (51 rows incl. header)
- `use-cases.md` — formal use cases derived from happy_path + error_path scenarios
- `edge-cases.md` — focused edge-case + failure-mode subset with severity ratings
- `summary.md` (this file)
