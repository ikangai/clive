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

PITFALLS:
- rg ignores .gitignored files by default; pass --no-ignore to include them

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
    text = generate_driver("rg", ExplorationResult(tool_name="rg",
        probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))
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
        generate_driver("rg", ExplorationResult(tool_name="rg",
            probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))


def test_generate_driver_rejects_section_only_in_prose(monkeypatch):
    # Section names appear only as substrings inside prose, never at line start.
    bad = """\
---
preferred_mode: script
---
# rg Driver

This driver covers ENVIRONMENT PRIMARY TOOLS PATTERNS PITFALLS RESPONSE FORMAT COMPLETION
but none as actual section headings.
"""
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError):
        generate_driver("rg", ExplorationResult(tool_name="rg",
            probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))


def test_generate_driver_rejects_no_frontmatter(monkeypatch):
    bad = (
        "# rg Driver\n\nENVIRONMENT: x\nPRIMARY TOOLS:\n- x\n"
        "PATTERNS:\n- x\nPITFALLS:\n- x\nRESPONSE FORMAT:\n- x\n"
        "COMPLETION: DONE: y\n"
    )
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError, match="frontmatter"):
        generate_driver("rg", ExplorationResult(tool_name="rg",
            probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))


# ─── PITFALLS required (gh#41 debug Bug 4) ──────────────────────────────────
# The synthesizer prompt template lists PITFALLS as a section but the
# validator previously omitted it from _REQUIRED_SECTIONS — letting the
# safety-warnings section drop silently.

def test_generate_driver_rejects_missing_pitfalls(monkeypatch):
    # Driver has every section EXCEPT PITFALLS.
    bad = """\
---
preferred_mode: script
---
# rg Driver

ENVIRONMENT: ripgrep
PRIMARY TOOLS:
- rg PATTERN
PATTERNS:
- case insensitive
RESPONSE FORMAT:
- bash block
COMPLETION: DONE: <summary>
"""
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError, match="missing section.*PITFALLS|PITFALLS.*missing"):
        generate_driver("rg", ExplorationResult(tool_name="rg",
            probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))


_VALID_DRIVER_WITH_PITFALLS = """\
---
preferred_mode: script
---
# rg Driver

ENVIRONMENT: ripgrep
PRIMARY TOOLS:
- rg PATTERN
PATTERNS:
- case insensitive
PITFALLS:
- watch out for huge .gitignore
RESPONSE FORMAT:
- bash block
COMPLETION: DONE: <summary>
"""


# ─── Validator strictness (gh#41 debug Bug 10) ──────────────────────────────
# Reject drivers with duplicate section markers, section markers inside
# fenced code blocks (decoys), or sections out of canonical order — these
# are the structural surface that smuggled-payload drivers exploit.

def test_generate_driver_rejects_duplicate_sections(monkeypatch):
    bad = _VALID_DRIVER_WITH_PITFALLS + "\nENVIRONMENT: second occurrence\n"
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError, match="duplicate section"):
        generate_driver("rg", ExplorationResult(tool_name="rg",
            probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))


def test_generate_driver_rejects_section_only_inside_fenced_code(monkeypatch):
    # All five required sections appear ONLY inside fenced code blocks
    # (which should be stripped before scanning) — must reject.
    bad = """\
---
preferred_mode: script
---
# rg Driver

```
ENVIRONMENT: decoy
PRIMARY TOOLS: decoy
PATTERNS: decoy
PITFALLS: decoy
RESPONSE FORMAT: decoy
COMPLETION: DONE: decoy
```

The real driver content is missing.
"""
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError, match="missing section"):
        generate_driver("rg", ExplorationResult(tool_name="rg",
            probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))


def test_generate_driver_rejects_sections_out_of_canonical_order(monkeypatch):
    # Sections present but COMPLETION appears before ENVIRONMENT — refuse.
    bad = """\
---
preferred_mode: script
---
# rg Driver

COMPLETION: DONE: out of order
RESPONSE FORMAT: bash
PITFALLS: none
PATTERNS: none
PRIMARY TOOLS: rg
ENVIRONMENT: ripgrep
"""
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError, match="out of canonical order|order"):
        generate_driver("rg", ExplorationResult(tool_name="rg",
            probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))


def test_generate_driver_accepts_valid_driver_with_pitfalls(monkeypatch):
    _stub_chat(monkeypatch, _VALID_DRIVER_WITH_PITFALLS)
    text = generate_driver("rg", ExplorationResult(tool_name="rg",
        probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))
    assert text.startswith("---")


# ─── Empty-result guard (gh#41 debug Bug 11) ────────────────────────────────
# generate_driver previously sent zero-probe ExplorationResults to the LLM,
# which would invent plausible-looking PRIMARY TOOLS from the tool name
# alone — a confidently-wrong driver lands on disk silently.

def test_generate_driver_refuses_empty_exploration_result(monkeypatch):
    # No probes, no summary — nothing to synthesize from.
    stub_calls = []
    def fake_chat(*a, **kw):
        stub_calls.append(a)
        return (_VALID_DRIVER_WITH_PITFALLS, 100, 50)
    monkeypatch.setattr("discovery.generator.chat", fake_chat)
    monkeypatch.setattr("discovery.generator.get_client", lambda: MagicMock())
    with pytest.raises(ValueError, match="empty exploration|no .* probes|no signal"):
        generate_driver("rg", ExplorationResult(tool_name="rg"))
    assert stub_calls == [], "chat() must not be called for empty exploration"


def test_generate_driver_refuses_all_failed_probes_with_no_summary(monkeypatch):
    # All probes returned non-zero AND no DONE summary — still no signal.
    r = ExplorationResult(tool_name="rg",
        probes=[ProbeOutcome(command="rg --bad", exit_code=2, screen="error")])
    stub_calls = []
    def fake_chat(*a, **kw):
        stub_calls.append(a)
        return (_VALID_DRIVER_WITH_PITFALLS, 100, 50)
    monkeypatch.setattr("discovery.generator.chat", fake_chat)
    monkeypatch.setattr("discovery.generator.get_client", lambda: MagicMock())
    with pytest.raises(ValueError):
        generate_driver("rg", r)
    assert stub_calls == []


def test_generate_driver_accepts_failed_probes_with_done_summary(monkeypatch):
    # All probes failed but exploration LLM still DONE'd with a useful
    # summary — that IS signal; let the synthesizer proceed.
    r = ExplorationResult(tool_name="rg",
        probes=[ProbeOutcome(command="rg --bad", exit_code=2, screen="error")],
        summary="ripgrep — recursive grep that respects .gitignore")
    _stub_chat(monkeypatch, _VALID_DRIVER_WITH_PITFALLS)
    text = generate_driver("rg", r)
    assert text.startswith("---")


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

## PITFALLS
- ignores .gitignored files by default

## RESPONSE FORMAT
- bash block

## COMPLETION
DONE: <summary>
"""
    _stub_chat(monkeypatch, heading_form)
    text = generate_driver("rg", ExplorationResult(tool_name="rg",
        probes=[ProbeOutcome(command="rg --help", exit_code=0, screen="rg")]))
    assert text.startswith("---")
