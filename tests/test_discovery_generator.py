"""Tests for discovery.generator.generate_driver — LLM mocked throughout."""
from unittest.mock import MagicMock

import pytest

from discovery.generator import AUTO_GEN_HEADER, generate_driver
from discovery.models import ExplorationResult, ProbeOutcome


def _stub_chat(monkeypatch, response: str):
    """Stub discovery.generator.chat to return ``response`` and capture messages."""
    captured: dict = {}

    def fake_chat(client, messages, **kw):
        captured["messages"] = messages
        return (response, 100, 50)

    monkeypatch.setattr("discovery.generator.chat", fake_chat)
    monkeypatch.setattr("discovery.generator.get_client", lambda: MagicMock())
    return captured


_VALID_DRIVER = """\
---
preferred_mode: script
agent_model: fast
observation_model: fast
---
# rg Driver

ENVIRONMENT: ripgrep
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
- rg PATTERN [PATH]: search recursively

PATTERNS:
- rg -i for case insensitive

RESPONSE FORMAT:
- bash block

COMPLETION: DONE: <summary>
"""


def test_generate_driver_returns_text_starting_with_frontmatter(monkeypatch):
    _stub_chat(monkeypatch, _VALID_DRIVER)
    r = ExplorationResult(tool_name="rg", probes=[
        ProbeOutcome(command="rg --help", exit_code=0, screen="ripgrep usage..."),
    ])
    text = generate_driver("rg", r)
    # Frontmatter must be at byte 0 — _parse_driver_frontmatter requires it.
    assert text.startswith("---")


def test_generate_driver_header_inside_body_not_before_frontmatter(monkeypatch):
    _stub_chat(monkeypatch, _VALID_DRIVER)
    text = generate_driver("rg", ExplorationResult(tool_name="rg"))
    front_end = text.find("---", 3)
    assert front_end > 0, "no closing frontmatter delimiter"
    front_block = text[: front_end + 3]
    body = text[front_end + 3:]
    # Header must NOT appear before / inside the frontmatter (would break the parser).
    assert AUTO_GEN_HEADER not in front_block
    # Header MUST appear in the body, near the top.
    assert AUTO_GEN_HEADER in body[:300]


def test_generate_driver_passes_exploration_history_to_llm(monkeypatch):
    captured = _stub_chat(monkeypatch, _VALID_DRIVER)
    r = ExplorationResult(tool_name="rg", probes=[
        ProbeOutcome(command="rg --help", exit_code=0, screen="ripgrep usage..."),
    ])
    generate_driver("rg", r)
    full = "\n".join(m["content"] for m in captured["messages"])
    assert "rg --help" in full
    assert "ripgrep usage" in full


def test_generate_driver_rejects_missing_section(monkeypatch):
    # PATTERNS section removed.
    bad = _VALID_DRIVER.replace("PATTERNS:\n- rg -i for case insensitive\n\n", "")
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError, match="missing section|malformed"):
        generate_driver("rg", ExplorationResult(tool_name="rg"))


def test_generate_driver_rejects_section_only_in_prose(monkeypatch):
    # Section names appear only as substrings inside prose, never at line start.
    bad = """\
---
preferred_mode: script
---
# rg Driver

This driver covers ENVIRONMENT PRIMARY TOOLS PATTERNS RESPONSE FORMAT COMPLETION
but none as actual section headings.
"""
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError):
        generate_driver("rg", ExplorationResult(tool_name="rg"))


def test_generate_driver_rejects_no_frontmatter(monkeypatch):
    bad = (
        "# rg Driver\n\nENVIRONMENT: x\nPRIMARY TOOLS:\n- x\n"
        "PATTERNS:\n- x\nRESPONSE FORMAT:\n- x\nCOMPLETION: DONE: y\n"
    )
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError, match="frontmatter"):
        generate_driver("rg", ExplorationResult(tool_name="rg"))


def test_generate_driver_accepts_heading_form_for_sections(monkeypatch):
    # Sections written as `## ENVIRONMENT` (with markdown heading prefix) are valid.
    heading_form = """\
---
preferred_mode: script
---
# rg Driver

## ENVIRONMENT
ripgrep

## PRIMARY TOOLS
- rg PATTERN

## PATTERNS
- case insensitive

## RESPONSE FORMAT
- bash block

## COMPLETION
DONE: <summary>
"""
    _stub_chat(monkeypatch, heading_form)
    text = generate_driver("rg", ExplorationResult(tool_name="rg"))
    assert text.startswith("---")
