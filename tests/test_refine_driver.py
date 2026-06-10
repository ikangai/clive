"""Tests for discovery.refiner.refine_driver — LLM mocked throughout (gh#41 Phase 3)."""
import os
from unittest.mock import MagicMock

import pytest

from discovery.models import RefinementSignal
from discovery.refiner import REFINED_HEADER, refine_driver


_CURRENT_DRIVER = """\
---
preferred_mode: script
agent_model: fast
observation_model: fast
---
# jq Driver

ENVIRONMENT: jq JSON processor
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
- jq FILTER [FILE]: apply filter

PATTERNS:
- jq '.key' file.json

PITFALLS:
- none observed

RESPONSE FORMAT:
- bash block

COMPLETION: DONE: <summary>
"""

_REFINED_DRIVER = """\
---
preferred_mode: script
agent_model: fast
observation_model: fast
---
# jq Driver

ENVIRONMENT: jq JSON processor
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
- jq FILTER [FILE]: apply filter
- jq -r FILTER [FILE]: raw string output

PATTERNS:
- jq '.key' file.json
- jq -r '.[].name' for plain lists

PITFALLS:
- bare .key fails on top-level arrays; use .[] first

RESPONSE FORMAT:
- bash block

COMPLETION: DONE: <summary>
"""


def _stub_chat(monkeypatch, response: str):
    captured: dict = {}

    def fake_chat(client, messages, **kw):
        captured["messages"] = messages
        return (response, 100, 50)

    monkeypatch.setattr("discovery.refiner.chat", fake_chat)
    monkeypatch.setattr("discovery.refiner.get_client", lambda: MagicMock())
    return captured


def _failing_signal(**kw):
    defaults = dict(
        task_id="l5-jq-1",
        passed=False,
        detail="expected tool not used: jq; flags_correct=False",
        tool_expected="jq",
        tool_used=None,
        tool_correct=False,
        flags_correct=False,
        fallback_used=False,
        discovery_turns=2,
    )
    defaults.update(kw)
    return RefinementSignal(**defaults)


def _write_driver(tmp_path, name="jq", text=_CURRENT_DRIVER):
    path = tmp_path / f"{name}.md"
    path.write_text(text)
    return str(tmp_path)


class TestRefineDriver:
    def test_happy_path_returns_refined_text(self, monkeypatch, tmp_path):
        captured = _stub_chat(monkeypatch, _REFINED_DRIVER)
        drivers_dir = _write_driver(tmp_path)

        text = refine_driver("jq", [_failing_signal()], drivers_dir=drivers_dir)

        assert text.startswith("---")
        assert REFINED_HEADER in text
        # The refinement prompt must carry the current driver and the
        # failure detail, with untrusted segments wrapped.
        user_msg = captured["messages"][-1]["content"]
        assert "jq FILTER [FILE]" in user_msg
        assert "expected tool not used" in user_msg
        assert "UNTRUSTED" in user_msg and "DO-NOT-FOLLOW" in user_msg

    def test_header_inside_body_not_before_frontmatter(self, monkeypatch, tmp_path):
        _stub_chat(monkeypatch, _REFINED_DRIVER)
        drivers_dir = _write_driver(tmp_path)
        text = refine_driver("jq", [_failing_signal()], drivers_dir=drivers_dir)
        # Frontmatter stays at byte 0; header after the closing ---.
        assert text.startswith("---")
        front_end = text.find("---", 3)
        assert REFINED_HEADER not in text[:front_end]
        assert REFINED_HEADER in text[front_end:]

    def test_refuses_when_all_signals_pass(self, monkeypatch, tmp_path):
        _stub_chat(monkeypatch, _REFINED_DRIVER)
        drivers_dir = _write_driver(tmp_path)
        ok = _failing_signal(passed=True, tool_correct=True, flags_correct=True, detail="")
        with pytest.raises(ValueError, match="no failure"):
            refine_driver("jq", [ok], drivers_dir=drivers_dir)

    def test_refuses_on_empty_signals(self, monkeypatch, tmp_path):
        _stub_chat(monkeypatch, _REFINED_DRIVER)
        drivers_dir = _write_driver(tmp_path)
        with pytest.raises(ValueError, match="no failure"):
            refine_driver("jq", [], drivers_dir=drivers_dir)

    def test_missing_driver_raises(self, monkeypatch, tmp_path):
        _stub_chat(monkeypatch, _REFINED_DRIVER)
        with pytest.raises(FileNotFoundError):
            refine_driver(
                "jq", [_failing_signal()],
                drivers_dir=str(tmp_path), unreviewed_dir=str(tmp_path / "none"),
            )

    def test_falls_back_to_unreviewed_dir(self, monkeypatch, tmp_path):
        _stub_chat(monkeypatch, _REFINED_DRIVER)
        unreviewed = tmp_path / ".unreviewed"
        unreviewed.mkdir()
        (unreviewed / "jq.md").write_text(_CURRENT_DRIVER)

        text = refine_driver(
            "jq", [_failing_signal()],
            drivers_dir=str(tmp_path), unreviewed_dir=str(unreviewed),
        )
        assert text.startswith("---")

    def test_unsafe_name_rejected_before_any_io(self, monkeypatch, tmp_path):
        _stub_chat(monkeypatch, _REFINED_DRIVER)
        with pytest.raises(ValueError, match="unsafe tool name"):
            refine_driver("Bad.Name", [_failing_signal()], drivers_dir=str(tmp_path))

    def test_reserved_name_rejected(self, monkeypatch, tmp_path):
        _stub_chat(monkeypatch, _REFINED_DRIVER)
        with pytest.raises(ValueError, match="reserved"):
            refine_driver("shell", [_failing_signal()], drivers_dir=str(tmp_path))

    def test_invalid_llm_output_rejected(self, monkeypatch, tmp_path):
        # Missing PITFALLS section — must fail structural validation.
        broken = _REFINED_DRIVER.replace("PITFALLS:\n- bare .key fails on top-level arrays; use .[] first\n\n", "")
        _stub_chat(monkeypatch, broken)
        drivers_dir = _write_driver(tmp_path)
        with pytest.raises(ValueError, match="PITFALLS"):
            refine_driver("jq", [_failing_signal()], drivers_dir=drivers_dir)


class TestRefinementSignal:
    def test_from_eval_result_duck_typed(self):
        """Converts any object carrying the ToolEvalResult fields — no
        import of evals/ from src/clive/."""

        class FakeToolEvalResult:
            task_id = "l5-jq-2"
            passed = False
            detail = "flags wrong"
            tool_used = "jq"
            tool_expected = "jq"
            tool_correct = True
            flags_correct = False
            fallback_used = False
            discovery_turns = 1

        sig = RefinementSignal.from_eval_result(FakeToolEvalResult())
        assert sig.task_id == "l5-jq-2"
        assert sig.passed is False
        assert sig.flags_correct is False
        assert sig.is_failure

    def test_is_failure_semantics(self):
        assert _failing_signal().is_failure
        ok = _failing_signal(passed=True, tool_correct=True, flags_correct=True)
        assert not ok.is_failure
        # A pass that needed an UNEXPECTED fallback is still a refinement signal.
        fb = _failing_signal(passed=True, tool_correct=True, flags_correct=True, fallback_used=True)
        assert fb.is_failure
        # ...but a fallback eval (tool deliberately disabled) expects one.
        fb_ok = _failing_signal(
            passed=True, tool_correct=True, flags_correct=True,
            fallback_used=True, fallback_expected=True,
        )
        assert not fb_ok.is_failure
