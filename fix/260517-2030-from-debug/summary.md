# Fix Session Summary — 2026-05-17

## Stats

- Session: `fix/260517-2030-from-debug/`
- Source: `debug/260517-2024-hunt-all-bugs/` (4 bugs found)
- Iterations: 4 successful fixes + 2 discarded approaches on Bug A
- Baseline: bare `pytest` aborted collection (2 errors). Manual workaround: 960 passed with `--ignore` flags.
- Final: bare `pytest` → **964 passed, 0 errors** (4 new lifecycle tests added with Bug B).
- All four bugs from the debug session are closed.

## Fix Score

`fix_score ≈ 100/100`
- Reduction: 60/60 (all 4 of 4 bugs closed)
- Guard: 25/25 (final `pytest` passes; no regressions introduced)
- Bonus: +15 (zero anti-patterns; +4 new tests covering the lifecycle that was previously untestable)

## Fixed

| Bug | Severity | Commit | What |
|-----|----------|--------|------|
| A | HIGH | `d5b2e65` | Bare `pytest` runs again — added `evals/conftest.py` and made the repo-root `clive.py` wrapper re-export `_is_direct`/`run` when imported as a module. |
| C | MEDIUM | `29c13cb` | Five `tui_*` shims now import cleanly from a cold subprocess — sibling references inside `tui/` switched to relative imports. |
| D | LOW | `b4dafc0` | Removed stale `get_toolset` line from `session/toolsets.py` docstring. |
| B | HIGH | `59d1d88` | Added `PaneStream.unsubscribe(q)` and wrapped both caller sites in `interactive_runner.py` with `try/finally` so per-turn and per-subtask queues are removed from the fan-out list on every return path. 4 new lifecycle tests. |

## Compound discovery — surfaced and resolved as part of Bug A

Fixing the collection abort revealed 15 latent failures in `tests/test_planner_bypass.py` (all `ImportError: cannot import name '_is_direct' from 'clive'`). Root cause: pytest's collection walks `evals/__init__.py` and prepends the repo root to `sys.path` AFTER `tests/conftest.py` runs — so `import clive` resolves to the repo-root wrapper instead of `src/clive/clive.py`. The wrapper didn't re-export the test-facing symbols.

Tried two approaches that didn't work:

1. **`pyproject.toml` with `pythonpath = ["src/clive", "."]`** — failed. `pythonpath` did not win the race against pytest's package-rootpath prepend, AND introduced 22 new failures in `test_slash_registry.py`/`test_tui_dashboard.py` (likely changed rootdir / fixture-resolution semantics). Reverted.
2. **`pytest_configure` hook re-inserting `src/clive` at sys.path[0]** — failed. By the time `pytest_configure` ran, the imports had not yet happened — but the inserted path was undone again later when collection walked `evals/__init__.py`. Reverted.

The fix that worked: extend the wrapper itself. Three lines, no global pytest config, no race to worry about.

## Bug B — design decision

The debug session flagged Bug B as needing design discussion (weak refs vs explicit unsubscribe). Picked **explicit `unsubscribe(q)`** because:

- The leak's root cause is *missing teardown at a known site*, not lifetime ambiguity — both caller sites have an obvious finalize moment (function return / coroutine cancellation), so a `finally:` block is the natural fit and keeps the read loop allocation-free.
- WeakSet of queues would require the caller to keep a strong ref anyway (or the queue gets GC'd mid-await), defeating the simplification.
- Made `unsubscribe` idempotent (`try/except ValueError`) so callers don't need to guard the finally block, and so double-calling on cancellation paths is safe.

## Verification

```bash
$ python3 -m pytest --tb=no -q
…
964 passed in 45.21s
```

```bash
$ for s in tui_actions tui_commands tui_helpers tui_task_runner tui_theme; do
    python3 -c "import sys; sys.path.insert(0,'.'); sys.path.insert(0,'src/clive'); import $s; print('$s OK')" 2>&1 | tail -1
  done
tui_actions OK
tui_commands OK
tui_helpers OK
tui_task_runner OK
tui_theme OK
```

```bash
$ python3 clive.py --list-toolsets | head -3
Profiles (use with -t):

  minimal      (default)
```

`python3 clive.py --tui` launches the Textual UI and exits cleanly on Ctrl-C (verified by killing the subprocess after 1s).

## Remaining

None — all four bugs from the source debug session are closed.

## Files changed

```
clive.py                                                  (+6 lines)
evals/conftest.py                                         (new, 10 lines)
src/clive/session/toolsets.py                              (-1 line)
src/clive/tui/tui.py                                       (4 lines: flat → relative)
src/clive/tui/tui_commands.py                              (2 lines: flat → relative)
src/clive/tui/tui_task_runner.py                           (1 line: flat → relative)
src/clive/observation/fifo_stream.py                       (+8 lines: unsubscribe)
src/clive/execution/interactive_runner.py                  (+27/-19: try/finally at 2 sites)
tests/test_fifo_stream.py                                  (+46 lines: 2 lifecycle tests)
tests/test_interactive_runner_streaming.py                 (+65 lines: 2 caller-site tests)
```

Four commits, all hook-passing.
