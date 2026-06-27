"""Tests for the learned-tool memo WRITE seam in discovery.auto (gh#41 slice 2/2).

``_explore_async`` is the background auto-explore body. After it successfully
writes a generated driver it must persist a learned-tool memo via
``tool_memo.record_tool_memo`` so the next run's Tier-2 card can reuse the
known-good invocation. The seam is best-effort: it must never break the
existing auto-explore path, and it must NOT record anything when the driver
write fails.

explore_tool / generate_driver / write_generated_driver are monkeypatched on
``discovery.auto`` to deterministic stubs so no LLM, tmux, or real filesystem
driver write is involved. CLIVE_HOME is redirected to tmp_path so the real
~/.clive memo store is never touched.
"""
import pytest

from discovery import auto, tool_memo
from discovery.models import ExplorationResult, ProbeOutcome


@pytest.fixture(autouse=True)
def _redirect_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIVE_HOME", str(tmp_path))
    return tmp_path


def _stub_generate_and_write(monkeypatch, tmp_path, driver_text):
    """Patch generate_driver + write_generated_driver to deterministic stubs."""
    monkeypatch.setattr(auto, "generate_driver", lambda name, result: driver_text)
    monkeypatch.setattr(
        auto,
        "write_generated_driver",
        lambda name, text, drivers_dir=None: f"{drivers_dir}/{name}.md",
    )


# ── (a) known-good invocation: first successful probe wins ────────────────────

def test_explore_async_records_known_good_invocation(monkeypatch, tmp_path):
    # explore_tool returns a result whose first *successful* probe is the
    # known-good invocation that should land in the memo.
    result = ExplorationResult(tool_name="ripgrep")
    result.probes.append(ProbeOutcome(command="rg --bad", exit_code=2, screen="err"))
    result.probes.append(ProbeOutcome(command="rg --version", exit_code=0, screen="rg 14"))
    monkeypatch.setattr(auto, "explore_tool", lambda name: result)
    _stub_generate_and_write(
        monkeypatch, tmp_path, "ripgrep: recursive line search\nmore text\n"
    )

    auto._explore_async("ripgrep", drivers_dir=str(tmp_path))

    memo = tool_memo.load_tool_memo("ripgrep")
    assert memo is not None
    assert memo["invocation"] == "rg --version"
    # usage is the driver's synopsis — first non-empty stripped line.
    assert memo["usage"] == "ripgrep: recursive line search"


# ── (b) fallback: no successful probe -> invocation=tool_name + synopsis ───────

def test_explore_async_falls_back_to_tool_name_and_synopsis(monkeypatch, tmp_path):
    result = ExplorationResult(tool_name="ripgrep")
    result.probes.append(ProbeOutcome(command="rg --boom", exit_code=1, screen="err"))
    monkeypatch.setattr(auto, "explore_tool", lambda name: result)
    _stub_generate_and_write(
        monkeypatch, tmp_path, "\n  \nripgrep synopsis line\nignored\n"
    )

    auto._explore_async("ripgrep", drivers_dir=str(tmp_path))

    memo = tool_memo.load_tool_memo("ripgrep")
    assert memo is not None
    assert memo["invocation"] == "ripgrep"
    assert memo["usage"] == "ripgrep synopsis line"


# ── (c) write failure: NO memo recorded, and _explore_async does not raise ────

def test_explore_async_records_no_memo_when_write_fails(monkeypatch, tmp_path):
    result = ExplorationResult(tool_name="ripgrep")
    result.probes.append(ProbeOutcome(command="rg --version", exit_code=0, screen="ok"))
    monkeypatch.setattr(auto, "explore_tool", lambda name: result)
    monkeypatch.setattr(auto, "generate_driver", lambda name, r: "ripgrep synopsis\n")

    def _boom(name, text, drivers_dir=None):
        raise RuntimeError("driver already exists")

    monkeypatch.setattr(auto, "write_generated_driver", _boom)

    # Must not raise — best-effort, failure is logged + swallowed.
    auto._explore_async("ripgrep", drivers_dir=str(tmp_path))

    # The write failed before the memo seam, so nothing was recorded.
    assert tool_memo.load_tool_memo("ripgrep") is None
