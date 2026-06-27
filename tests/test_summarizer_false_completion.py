"""Deterministic false-completion gate in planning/summarizer.py (#40, task-eab35a12).

A subtask can be marked COMPLETED while its observable evidence contradicts the
success claim — most concretely, a non-zero ``exit_code`` (the agent said DONE,
but the last command failed). ``summarize()`` historically synthesised the
user-facing answer from each result's ``.summary`` at FACE VALUE, laundering such
an unsupported success into the final answer.

These tests pin two behaviours, both deterministic (chat/get_client monkeypatched):

  * ``detect_false_completion(results)`` flags only COMPLETED results whose
    evidence contradicts them (exit_code is an int and != 0), and returns the
    flags so the dead ``false_completion`` metric / eval scenarios can assert on
    them — with NO false positives (exit_code 0 or None, or non-COMPLETED status).
  * ``summarize()`` annotates each flagged result's line with a grounded
    ``[UNVERIFIED — exit_code=N; ...]`` caveat in the user message it sends to the
    summariser LLM, so the synthesised answer surfaces the discrepancy.
"""
from unittest.mock import MagicMock, patch

from models import SubtaskResult, SubtaskStatus
from planning import summarizer


def _result(sid, status, summary="done", exit_code=None, output_files=None):
    return SubtaskResult(
        subtask_id=sid,
        status=status,
        summary=summary,
        output_snippet="",
        exit_code=exit_code,
        output_files=output_files or [],
    )


def _completed(sid, exit_code=None, **kw):
    return _result(sid, SubtaskStatus.COMPLETED, exit_code=exit_code, **kw)


# --- detect_false_completion: the pure, deterministic helper -----------------

def test_detect_flags_completed_with_nonzero_exit():
    """COMPLETED but exit_code != 0 -> flagged (claims done, last command failed)."""
    results = [_completed("1", exit_code=1, summary="built the thing")]
    flags = summarizer.detect_false_completion(results)
    assert [sid for sid, _ in flags] == ["1"]
    _, reason = flags[0]
    assert "exit_code=1" in reason


def test_detect_ignores_zero_exit():
    """COMPLETED with exit_code == 0 is a supported success -> not flagged."""
    results = [_completed("1", exit_code=0)]
    assert summarizer.detect_false_completion(results) == []


def test_detect_ignores_none_exit():
    """exit_code unknown (None) is not evidence of failure -> not flagged."""
    results = [_completed("1", exit_code=None)]
    assert summarizer.detect_false_completion(results) == []


def test_detect_ignores_nonzero_exit_when_not_completed():
    """A FAILED result with exit_code != 0 is honest, not a *false* completion."""
    results = [_result("1", SubtaskStatus.FAILED, exit_code=1)]
    assert summarizer.detect_false_completion(results) == []


def test_detect_returns_flags_for_observability():
    """The helper RETURNS (subtask_id, reason) pairs so the dead false_completion
    metric / eval scenarios can assert on them without touching frozen code."""
    results = [
        _completed("a", exit_code=0),
        _completed("b", exit_code=2, summary="ran migration"),
        _result("c", SubtaskStatus.FAILED, exit_code=3),
    ]
    flags = summarizer.detect_false_completion(results)
    assert [sid for sid, _ in flags] == ["b"]


# --- summarize(): annotate the flagged result's line in the user message ------

def _summarize_capture(results):
    captured = {}

    def fake_chat(client, messages, **kw):
        captured["messages"] = messages
        return "ok", 0, 0

    with patch.object(summarizer, "chat", fake_chat), \
            patch.object(summarizer, "get_client", return_value=MagicMock()):
        summarizer.summarize(task="original", results=results)

    return next(m for m in captured["messages"] if m["role"] == "user")["content"]


def test_summarize_annotates_flagged_result_with_caveat():
    """A COMPLETED+exit_code=1 result's line carries the UNVERIFIED caveat in the
    user message built for the summariser LLM."""
    results = [_completed("1", exit_code=1, summary="created report.csv")]
    content = _summarize_capture(results)
    assert "[UNVERIFIED" in content
    assert "exit_code=1" in content
    # The original summary is still present — the caveat annotates, not replaces.
    assert "created report.csv" in content


def test_summarize_no_caveat_when_exit_code_zero():
    """A supported success (exit_code 0) is presented as-is, no caveat."""
    results = [_completed("1", exit_code=0, summary="all good")]
    content = _summarize_capture(results)
    assert "UNVERIFIED" not in content


def test_summarize_no_caveat_when_exit_code_none():
    """Unknown exit_code is not flagged -> no caveat (no false positives)."""
    results = [_completed("1", exit_code=None, summary="all good")]
    content = _summarize_capture(results)
    assert "UNVERIFIED" not in content
