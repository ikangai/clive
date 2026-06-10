# Collapse the flat-import shim layer: make `src/clive/` subpackages the real import path.

**Thesis:** The next architectural step is to delete the ~30 single-line `sys.modules` shims at the root of `src/clive/` and migrate every `from executor import ...` / `from planner import ...` call site to `from execution.executor import ...` / `from planning.planner import ...`. This is the precondition for every subsequent move Clive's roadmap implies (rooms, lobby, evolution, selfmod) and it costs us nothing we want to keep.

## The locus

Today `src/clive/` contains two parallel namespaces for the same code:

- Real implementations: `src/clive/execution/executor.py` (274 lines), `planning/planner.py` (125 lines), `execution/script_runner.py`, `execution/room_runner.py`, `execution/runtime.py`, and siblings.
- One-line shims at the flat root: `executor.py`, `planner.py`, `script_runner.py`, `room_runner.py`, `runtime.py`, etc., each literally `sys.modules[__name__] = importlib.import_module("execution.executor")`.

CLAUDE.md frames this as "Subpackages are organizational, not import-path scoping." In practice it is the opposite: the subpackage tree is the *fiction*, and the actual import graph is flat. `clive_core.py:29`, `router.py:17`, `cli_modes.py:17`, `tui/tui_task_runner.py`, and even `execution/interactive_runner.py:224` (a sibling reaching across the shim to its own package!) all import flat.

## Rationale tied to current state

Three forces have made this load-bearing rather than cosmetic:

1. **Name collisions are now arriving.** `execution/room_runner.py` and a flat `room_runner.py` shim both exist; same for `runtime.py`, `skill_runner.py`, `speculative.py`, `pane_loop.py`. The brief lists lobby/rooms/evolution/selfmod as live experimental subsystems, and `repo-restructure` appears in the 2026-04-14 plan list. Every new module added to `execution/` or `networking/` must also remember to spawn a flat shim, or imports silently break — exactly the failure mode that produced the 2026-04-09 "relative imports for tui_* siblings" fix already on main.

2. **Sibling-package imports are reaching through the shim.** `execution/interactive_runner.py` does `from executor import handle_agent_pane_frame` — a module inside `execution/` round-tripping through a root-level shim to reach `execution/executor`. That is two import-system features (sys.path injection in `clive.py`/`conftest.py` plus runtime `sys.modules` overwrite) collaborating to fake a flat namespace. Static analyzers, IDE jump-to-definition, and `importlib.reload()` all break in the presence of `sys.modules` reassignment. The recent "unbreak bare pytest" fix is a symptom of exactly this fragility.

3. **The shim layer blocks `pip install`.** Clive cannot ship as a normal Python package while `clive.py` + `conftest.py` are required to inject `src/clive/` onto `sys.path`. As soon as Clive is invoked from a remote host's `~/.clive/instances/` registry (which the brief already lists as live), or as soon as BYOLLM delegate clients want to import `llm/delegate_client.py` from outside the repo, the flat-import contract leaks. Dotted imports are the only way out.

## The concrete change

1. Rewrite every `from <flatname> import X` call site to its dotted form (`from execution.executor import X`, `from planning.planner import X`, etc.). The mapping is mechanical — each shim file already names its target.
2. Delete the ~30 shim files at `src/clive/` root.
3. Keep `src/clive/` on `sys.path` via `clive.py` and `tests/conftest.py` for now (the subpackages stay reachable as `execution`, `planning`, `networking`, etc.). This is a one-step refactor, not a packaging overhaul.
4. Add a CI grep guard: `! grep -rE "^from (executor|planner|script_runner|runtime|room_runner) import" src/`.

## Tradeoff accepted

I am accepting a **single large mechanical diff** (every call site touched) in exchange for permanently removing a class of bug (shim/real divergence, sibling-through-shim imports, `sys.modules` reassignment) and unblocking the eventual pivot to a proper installable package. I am explicitly *not* doing the bigger move — converting to `from clive.execution.executor import ...` with a real package root — because that would require rewriting `clive.py`, `conftest.py`, and every test, and it is not yet forced. The shim collapse is forced now; full packaging is not.

What this is *not*: it is not "cleanup," it is removing a parallel namespace that is actively producing fix commits on main and that will compound as `rooms/`, `lobby_*`, and `evolution/` graduate from experimental.
