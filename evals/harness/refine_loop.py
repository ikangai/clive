"""Layer 5 → refine_driver orchestration (gh#40 closing the gh#41 loop).

Runs the discovery evals, converts failures into RefinementSignals, asks
``discovery.refiner.refine_driver`` for a revised driver, and writes it
through the normal quarantine path. The promotion decision stays with the
operator (or a future re-run-and-compare step):

    python3 evals/harness/run_eval.py --layer 5 --refine jq

Direction of dependency: this module lives on the evals side and imports
from ``src/clive``; ``src/clive`` never imports the eval harness
(RefinementSignal.from_eval_result duck-types ToolEvalResult).
"""
from __future__ import annotations


def signals_for_tool(tool_name: str, results: list) -> list:
    """Convert eval results that targeted ``tool_name`` into signals.

    A result targets the tool when its tool_expected alternation (e.g.
    "rg|grep") contains the name. Results without tool_expected are
    skipped — they carry no per-tool evidence.
    """
    from discovery.models import RefinementSignal

    signals = []
    for r in results:
        expected = getattr(r, "tool_expected", None)
        if not expected:
            continue
        if tool_name in [alt.strip() for alt in expected.split("|")]:
            signals.append(RefinementSignal.from_eval_result(r))
    return signals


def refine_from_results(tool_name: str, results: list) -> str | None:
    """Refine ``tool_name``'s driver from eval results; quarantine-write.

    Returns the quarantine path of the refined driver, or None when there
    is nothing to do (no matching signals, no failures among them, or no
    existing driver to refine — hand-written RESERVED drivers refuse by
    name check inside refine_driver).
    """
    from discovery.refiner import refine_driver
    from discovery.generator import write_generated_driver

    signals = signals_for_tool(tool_name, results)
    if not signals:
        return None
    if not any(s.is_failure for s in signals):
        return None
    try:
        text = refine_driver(tool_name, signals)
    except (ValueError, FileNotFoundError):
        # Reserved/unsafe name, no failures, or no driver on disk —
        # all legitimate "nothing to refine" outcomes for a sweep.
        return None
    return write_generated_driver(tool_name, text, overwrite=True)
