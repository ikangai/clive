"""Contract test pinning the ``probe`` event between ``interactive_runner``
and ``discovery.explorer`` (gh#41 debug Bug 8).

The discovery explorer relies on ``run_subtask_interactive`` emitting a
``probe`` event with the shape ``(subtask_id, cmd, exit_code, prev_screen)``
per turn. A refactor of the runner that renames the event, drops it, or
changes its arity silently breaks every ``--explore`` invocation because
``ExplorationResult.probes`` would stay empty and the synthesizer would
hallucinate from nothing (compounds with the empty-result guard in Bug 11
once that fires — useful but masking the deeper regression).

This test pins the producer-consumer contract by:

1. AST-scanning ``execution/interactive_runner.py`` for the literal
   ``_emit(on_event, "probe", ...)`` call and asserting it has the
   expected number of positional arguments.
2. AST-scanning ``discovery/explorer.py`` for the consumer-side
   ``"probe"`` branch and asserting it unpacks the same number of args.

If either side moves, the assertion message names the file and the
expected shape so the developer immediately knows which half drifted.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Source files. Resolved via ``execution.interactive_runner.__file__`` /
# ``discovery.explorer.__file__`` so the test follows the package wherever
# it lives.
from execution import interactive_runner
from discovery import explorer


# ─── Producer (interactive_runner) ──────────────────────────────────────────


def _find_emit_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every ``_emit(on_event, "probe", ...)`` Call node in tree."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_emit"):
            continue
        # First positional arg should be the on_event callback variable;
        # second should be the string literal event name.
        if len(node.args) < 2:
            continue
        ev = node.args[1]
        if isinstance(ev, ast.Constant) and ev.value == "probe":
            out.append(node)
    return out


def test_interactive_runner_emits_probe_event():
    """``run_subtask_interactive`` must call ``_emit(on_event, "probe", ...)``.

    The shape the consumer (discovery.explorer.on_event) unpacks is
    ``(subtask.id, cmd, exit_code, prev_screen)``. So the _emit call
    arity is: on_event + "probe" + 4 data args = 6 positional args.
    """
    src = Path(interactive_runner.__file__).read_text()
    tree = ast.parse(src)
    probe_emits = _find_emit_calls(tree)
    assert len(probe_emits) >= 1, (
        "execution/interactive_runner.py no longer emits a 'probe' event. "
        "discovery.explorer depends on this — restore the call "
        "_emit(on_event, 'probe', subtask.id, cmd, exit_code, prev_screen) "
        "or update both sides of the contract."
    )
    # Check arity of (at least one) probe emit.
    arities = [len(c.args) for c in probe_emits]
    assert 6 in arities, (
        f"execution/interactive_runner.py: 'probe' _emit call has unexpected "
        f"arity. Found arities {arities}; expected 6 (on_event + 'probe' + "
        f"subtask.id + cmd + exit_code + prev_screen). discovery.explorer "
        f"depends on this exact shape."
    )


# ─── Consumer (discovery.explorer) ──────────────────────────────────────────


def _find_probe_branch(tree: ast.AST) -> ast.If | None:
    """Find the ``if event_type == "probe":`` branch in explorer's on_event."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        # event_type == "probe"
        if (
            isinstance(t, ast.Compare)
            and isinstance(t.left, ast.Name) and t.left.id == "event_type"
            and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq)
            and len(t.comparators) == 1
            and isinstance(t.comparators[0], ast.Constant)
            and t.comparators[0].value == "probe"
        ):
            return node
    return None


def test_explorer_consumes_probe_event_with_matching_arity():
    """The ``"probe"`` branch in ``explore_tool.on_event`` must unpack 4 args.

    The runner emits ``(subtask.id, cmd, exit_code, prev_screen)`` — four
    payload fields. The consumer must unpack exactly those.
    """
    src = Path(explorer.__file__).read_text()
    tree = ast.parse(src)
    branch = _find_probe_branch(tree)
    assert branch is not None, (
        "discovery/explorer.py no longer has an `if event_type == 'probe':' "
        "branch. The runner emits 'probe' events that this branch is "
        "responsible for consuming."
    )
    # Look for the unpacking inside the branch: tuple assignment like
    # ``_sid, cmd, exit_code, screen = args`` with 4 targets.
    found_arity = None
    for node in ast.walk(branch):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Tuple):
                    found_arity = len(target.elts)
                    break
            if found_arity is not None:
                break
    assert found_arity == 4, (
        f"discovery/explorer.py 'probe' branch unpacks {found_arity} fields, "
        f"but the runner emits 4 (subtask.id, cmd, exit_code, prev_screen). "
        f"Update both sides of the contract together."
    )


# ─── Integration sanity (a real _emit call → real on_event callback) ────────


def test_emit_helper_forwards_probe_event_to_callback():
    """``_emit`` is the runner's delivery mechanism; verify it passes through
    a 'probe' event to the consumer's on_event callable with all 4 fields.

    This is a small belt-and-braces check on top of the AST tests: it
    exercises the runtime path so a future refactor of ``_emit`` itself
    (not just its callers) would also be caught.
    """
    from runtime import _emit

    received = []
    def on_event(*args):
        received.append(args)

    # Simulate what interactive_runner.py line 327 does.
    _emit(on_event, "probe", "subtask-id", "echo hi", 0, "echo hi\n[EXIT:0]\n")

    assert len(received) == 1
    event_type, sid, cmd, exit_code, screen = received[0]
    assert event_type == "probe"
    assert sid == "subtask-id"
    assert cmd == "echo hi"
    assert exit_code == 0
    assert "echo hi" in screen
