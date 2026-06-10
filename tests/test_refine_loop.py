"""Tests for the Layer 5 → refine_driver orchestration (gh#40/gh#41)."""
import pytest

from evals.harness.metrics import ToolEvalResult
from evals.harness.refine_loop import refine_from_results, signals_for_tool


def _result(task_id, passed, tool_expected, **kw):
    base = dict(
        task_id=task_id, layer=5, tool="discovery", passed=passed,
        turns_used=4, min_turns=2, prompt_tokens=1, completion_tokens=1,
        elapsed_seconds=0.1, detail="d", tool_expected=tool_expected,
    )
    base.update(kw)
    return ToolEvalResult(**base)


def test_signals_match_alternation_membership():
    results = [
        _result("a", False, "rg|grep"),
        _result("b", True, "jq"),
        _result("c", False, None),
        _result("d", False, "ripgrep"),  # not an exact alternative of rg
    ]
    sigs = signals_for_tool("rg", results)
    assert [s.task_id for s in sigs] == ["a"]
    assert signals_for_tool("jq", results)[0].task_id == "b"


def test_refine_skips_without_failures(monkeypatch):
    called = {}
    import discovery.refiner as refiner_mod
    monkeypatch.setattr(
        refiner_mod, "refine_driver",
        lambda *a, **k: called.setdefault("refine", True),
    )
    assert refine_from_results("jq", [_result("a", True, "jq")]) is None
    assert "refine" not in called


def test_refine_writes_through_quarantine(monkeypatch):
    import discovery.refiner as refiner_mod
    import discovery.generator as gen_mod
    monkeypatch.setattr(
        refiner_mod, "refine_driver", lambda name, signals: "REFINED-TEXT"
    )
    seen = {}

    def fake_write(name, text, overwrite=False):
        seen.update(name=name, text=text, overwrite=overwrite)
        return f"/quarantine/{name}.md"

    monkeypatch.setattr(gen_mod, "write_generated_driver", fake_write)
    path = refine_from_results("jq", [_result("a", False, "jq")])
    assert path == "/quarantine/jq.md"
    assert seen == {"name": "jq", "text": "REFINED-TEXT", "overwrite": True}


def test_refine_tolerates_missing_driver(monkeypatch):
    import discovery.refiner as refiner_mod

    def boom(name, signals):
        raise FileNotFoundError("no driver")

    monkeypatch.setattr(refiner_mod, "refine_driver", boom)
    assert refine_from_results("jq", [_result("a", False, "jq")]) is None
