"""Tests for discovery.tool_memo — persistent learned-tool JSON cache (gh#41).

CLIVE_HOME is redirected to tmp_path so the real ~/.clive is never touched.
The override is read INSIDE the functions, so monkeypatching the env works.
"""
import pytest

from discovery import tool_memo


@pytest.fixture(autouse=True)
def _redirect_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIVE_HOME", str(tmp_path))
    return tmp_path


# ── (a) round-trip ────────────────────────────────────────────────────────────

def test_record_then_load_round_trips(_redirect_home):
    tool_memo.record_tool_memo(
        "fzf", "fzf --height 40%", "fuzzy-find lines from stdin"
    )
    memo = tool_memo.load_tool_memo("fzf")
    assert memo is not None
    assert memo["invocation"] == "fzf --height 40%"
    assert memo["usage"] == "fuzzy-find lines from stdin"


def test_record_merges_multiple_tools(_redirect_home):
    tool_memo.record_tool_memo("fzf", "fzf", "fuzzy find")
    tool_memo.record_tool_memo("bat", "bat file.py", "cat with syntax highlight")
    assert tool_memo.load_tool_memo("fzf")["invocation"] == "fzf"
    assert tool_memo.load_tool_memo("bat")["invocation"] == "bat file.py"


# ── (b) memo_card contains the invocation ─────────────────────────────────────

def test_memo_card_contains_invocation(_redirect_home):
    tool_memo.record_tool_memo("fzf", "fzf --height 40%", "fuzzy-find lines")
    card = tool_memo.memo_card("fzf")
    assert card is not None
    assert "fzf --height 40%" in card
    assert "fuzzy-find lines" in card


# ── (c) None on missing / corrupt — never raises ──────────────────────────────

def test_load_returns_none_when_no_memo(_redirect_home):
    assert tool_memo.load_tool_memo("never_recorded") is None
    assert tool_memo.memo_card("never_recorded") is None


def test_load_returns_none_for_absent_key(_redirect_home):
    tool_memo.record_tool_memo("fzf", "fzf", "fuzzy find")
    assert tool_memo.load_tool_memo("bat") is None
    assert tool_memo.memo_card("bat") is None


def test_corrupt_json_returns_none_and_does_not_raise(_redirect_home):
    home = _redirect_home
    (home / "tool_memos.json").write_text("{not valid json at all }}}")
    # Must not raise:
    assert tool_memo.load_tool_memo("fzf") is None
    assert tool_memo.memo_card("fzf") is None


# ── (d) read seam in toolsets.build_tier2_card ────────────────────────────────

def test_build_tier2_card_returns_memo_for_learned_tool(_redirect_home):
    import toolsets

    # "fzf" is neither a COMMAND nor a PANE — a genuinely learned/unknown tool.
    assert "fzf" not in toolsets.COMMANDS
    assert "fzf" not in toolsets.PANES

    tool_memo.record_tool_memo("fzf", "fzf --height 40%", "fuzzy-find lines")
    card = toolsets.build_tier2_card("fzf")
    assert card is not None
    assert "fzf --height 40%" in card


def test_build_tier2_card_none_for_unknown_without_memo(_redirect_home):
    import toolsets

    assert toolsets.build_tier2_card("totally_unknown_tool_xyz") is None
