"""Tests for the LLM-native execution mode (execution/llm_runner.py).

The runner reads input files, calls the LLM once, writes the result, and
returns a SubtaskResult. These tests stub out the LLM call so they're
hermetic — no network, no real client.
"""

import os
import tempfile

import pytest

import llm_runner as mod
from models import PaneInfo, Subtask, SubtaskStatus


class _FakePane:
    """Minimal stand-in for PaneInfo — llm_runner only reads agent_model."""
    agent_model = None
    sandboxed = False


@pytest.fixture
def session_dir(tmp_path):
    d = tmp_path / "session"
    d.mkdir()
    return str(d)


@pytest.fixture
def stub_chat(monkeypatch):
    """Capture the call args; reply is configurable per-test via .reply."""

    class _Stub:
        reply = "translated text"
        raises = None
        calls: list = []

        def __call__(self, client, messages, max_tokens=None, model=None, temperature=None):
            self.calls.append({"messages": messages, "max_tokens": max_tokens, "model": model})
            if self.raises:
                raise self.raises
            return self.reply, 10, 20

    stub = _Stub()
    monkeypatch.setattr(mod, "chat", stub)
    monkeypatch.setattr(mod, "get_client", lambda: object())
    return stub


def _run(description, session_dir, sid="1"):
    s = Subtask(id=sid, description=description, pane="shell", mode="llm")
    return mod.run_subtask_llm(s, _FakePane(), dep_context="", session_dir=session_dir)


# ── Happy paths ──────────────────────────────────────────────────────────────

def test_reads_session_file_and_writes_output(session_dir, stub_chat):
    with open(os.path.join(session_dir, "transcript.txt"), "w") as f:
        f.write("hello world")
    stub_chat.reply = "hallo welt"

    r = _run("translate transcript.txt into german", session_dir, sid="42")

    assert r.status == SubtaskStatus.COMPLETED
    assert r.turns_used == 1
    assert r.prompt_tokens == 10 and r.completion_tokens == 20
    out = os.path.join(session_dir, "llm_42.txt")
    assert os.path.isfile(out)
    with open(out) as f:
        assert f.read() == "hallo welt"
    # The input file content must have been forwarded to the model.
    user_msg = stub_chat.calls[-1]["messages"][1]["content"]
    assert "hello world" in user_msg
    assert "FILE: transcript.txt" in user_msg


def test_reads_absolute_path_in_description(session_dir, stub_chat, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    src = other / "doc.md"
    src.write_text("english content")
    stub_chat.reply = "summary"

    r = _run(f"summarize {src} in one sentence", session_dir)

    assert r.status == SubtaskStatus.COMPLETED
    user_msg = stub_chat.calls[-1]["messages"][1]["content"]
    assert "english content" in user_msg


def test_strips_done_footer_from_output_file(session_dir, stub_chat):
    stub_chat.reply = "real body of the answer\n---\nDONE: produced a short answer"

    r = _run("summarize this", session_dir)

    out = os.path.join(session_dir, "llm_1.txt")
    with open(out) as f:
        content = f.read()
    assert content == "real body of the answer"
    assert "DONE:" not in content
    assert r.summary == "produced a short answer"


def test_strips_wrapping_code_fence(session_dir, stub_chat):
    stub_chat.reply = "```\nplain body\n```"

    _run("rewrite", session_dir)

    with open(os.path.join(session_dir, "llm_1.txt")) as f:
        assert f.read() == "plain body"


def test_inline_task_with_no_files_still_runs(session_dir, stub_chat):
    stub_chat.reply = "a haiku\nabout the moon\nin three lines"

    r = _run("write me a haiku about the moon", session_dir)

    assert r.status == SubtaskStatus.COMPLETED
    user_msg = stub_chat.calls[-1]["messages"][1]["content"]
    assert "no input files found" in user_msg


# ── Failure modes ────────────────────────────────────────────────────────────

def test_llm_call_exception_becomes_failed_result(session_dir, stub_chat):
    stub_chat.raises = RuntimeError("network down")

    r = _run("anything", session_dir)

    assert r.status == SubtaskStatus.FAILED
    assert "network down" in (r.error or "")
    assert "network down" in r.summary


def test_empty_output_is_failure(session_dir, stub_chat):
    stub_chat.reply = "   "

    r = _run("anything", session_dir)

    assert r.status == SubtaskStatus.FAILED
    assert "empty" in r.summary.lower()


def test_write_failure_becomes_failed_result(session_dir, stub_chat, monkeypatch):
    stub_chat.reply = "body"

    def boom(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_file", boom)

    r = _run("anything", session_dir)

    assert r.status == SubtaskStatus.FAILED
    assert "disk full" in (r.error or "")


# ── Safety / symmetry ────────────────────────────────────────────────────────

def test_internal_files_are_not_fed_as_input(session_dir, stub_chat):
    """Files prefixed with `_` are Clive's scratch files; never forward them."""
    with open(os.path.join(session_dir, "_script_internal.sh"), "w") as f:
        f.write("SECRET_MARKER")
    with open(os.path.join(session_dir, "real.txt"), "w") as f:
        f.write("user content")
    stub_chat.reply = "ok"

    _run("process real.txt", session_dir)

    user_msg = stub_chat.calls[-1]["messages"][1]["content"]
    assert "user content" in user_msg
    assert "SECRET_MARKER" not in user_msg


def test_previous_llm_output_not_fed_back_as_input(session_dir, stub_chat):
    """If llm_1.txt already exists, the runner must not loop it into its own prompt."""
    with open(os.path.join(session_dir, "llm_1.txt"), "w") as f:
        f.write("OLD OUTPUT FROM LAST RUN")
    with open(os.path.join(session_dir, "input.txt"), "w") as f:
        f.write("fresh input")
    stub_chat.reply = "new body"

    _run("translate input.txt", session_dir, sid="1")

    user_msg = stub_chat.calls[-1]["messages"][1]["content"]
    assert "fresh input" in user_msg
    assert "OLD OUTPUT FROM LAST RUN" not in user_msg


def test_binary_session_files_are_skipped(session_dir, stub_chat):
    with open(os.path.join(session_dir, "blob.bin"), "wb") as f:
        f.write(b"\x00\x01\x02\x03binary")
    with open(os.path.join(session_dir, "doc.txt"), "w") as f:
        f.write("plain")
    stub_chat.reply = "ok"

    _run("summarize", session_dir)

    user_msg = stub_chat.calls[-1]["messages"][1]["content"]
    assert "plain" in user_msg
    # Binary header shouldn't leak into the prompt.
    assert "\x00" not in user_msg


def test_max_output_tokens_is_env_overridable(session_dir, stub_chat, monkeypatch):
    monkeypatch.setenv("CLIVE_LLM_OUTPUT_TOKENS", "2048")
    stub_chat.reply = "ok"

    _run("anything", session_dir)

    assert stub_chat.calls[-1]["max_tokens"] == 2048


def test_uses_pane_agent_model_override(session_dir, stub_chat):
    class _Pinned(_FakePane):
        agent_model = "some-cheap-model"

    s = Subtask(id="9", description="x", pane="shell", mode="llm")
    mod.run_subtask_llm(s, _Pinned(), dep_context="", session_dir=session_dir)

    assert stub_chat.calls[-1]["model"] == "some-cheap-model"
