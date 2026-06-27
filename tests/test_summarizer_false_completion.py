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


# --- SLICE 2: classifier-model DONE-verification judge -----------------------
#
# The deterministic gate only catches a COMPLETED result with a *contradicting*
# exit_code. A result that exits 0 (or unknown) yet whose summary does not
# actually satisfy the task slips through. For exactly those not-yet-flagged
# COMPLETED results, judge_false_completion() makes ONE cheap classifier-model
# call grounded in OBSERVABLE evidence; a NEGATIVE verdict is treated like a
# deterministic flag (UNVERIFIED caveat + returned flag). Cost is bounded: at
# most one judge call per not-yet-flagged COMPLETED result, and none when there
# are none. All model calls are stubbed so the tests are deterministic.

SUPPORTED = '{"supported": true, "reason": "evidence matches the claim"}'
UNSUPPORTED = '{"supported": false, "reason": "no such file in the evidence"}'


def _run_summarize(results, judge_verdict=SUPPORTED, task="create report.csv"):
    """Drive summarize() with BOTH model calls stubbed. The judge call is the
    one that passes the classifier model (model kwarg set); the final synthesis
    call passes no model. Returns (summarizer_user_content, calls)."""
    calls = {"judge": [], "summary": []}

    def fake_chat(client, messages, **kw):
        if kw.get("model"):  # the judge uses the cheap classifier model
            calls["judge"].append(messages)
            return judge_verdict, 0, 0
        calls["summary"].append(messages)
        return "final answer", 0, 0

    with patch.object(summarizer, "chat", fake_chat), \
            patch.object(summarizer, "get_client", return_value=MagicMock()):
        summarizer.summarize(task=task, results=results)

    user = next(m for m in calls["summary"][-1] if m["role"] == "user")["content"]
    return user, calls


def test_summarize_judge_flags_unsupported_completion():
    """COMPLETED + exit_code=0 but the judge says NOT supported -> flagged with
    the UNVERIFIED caveat (the deterministic gate would have let it through)."""
    results = [_completed("1", exit_code=0, summary="created report.csv")]
    content, calls = _run_summarize(results, judge_verdict=UNSUPPORTED)
    assert len(calls["judge"]) == 1          # exactly one judge call
    assert "[UNVERIFIED" in content
    assert "created report.csv" in content    # caveat annotates, not replaces


def test_summarize_judge_supported_completion_not_flagged():
    """Same result, but the judge says supported -> NOT flagged, no caveat."""
    results = [_completed("1", exit_code=0, summary="created report.csv")]
    content, calls = _run_summarize(results, judge_verdict=SUPPORTED)
    assert len(calls["judge"]) == 1
    assert "UNVERIFIED" not in content


def test_judge_not_called_for_deterministically_flagged():
    """Cost guard: a result already flagged by detect_false_completion
    (COMPLETED + exit_code != 0) is NOT re-judged by the classifier."""
    results = [_completed("1", exit_code=1, summary="ran migration")]
    content, calls = _run_summarize(results, judge_verdict=UNSUPPORTED)
    assert calls["judge"] == []               # no judge call (cost guard)
    assert "[UNVERIFIED" in content           # still caveated deterministically


def test_judge_skipped_when_no_eligible_completions():
    """Cost guard: with no not-yet-flagged COMPLETED result the judge makes
    ZERO calls (skip entirely)."""
    results = [_result("1", SubtaskStatus.FAILED, summary="boom", exit_code=2)]
    _content, calls = _run_summarize(results, judge_verdict=UNSUPPORTED)
    assert calls["judge"] == []


def test_judge_message_is_evidence_grounded():
    """The judge call is grounded in OBSERVABLE evidence — the result's summary,
    output snippet and file previews — not free-form self-critique."""
    r = SubtaskResult(
        subtask_id="1",
        status=SubtaskStatus.COMPLETED,
        summary="wrote the parser",
        output_snippet="SyntaxError: unexpected EOF",
        exit_code=0,
        output_files=[{"path": "out/parser.py", "preview": "def parse(:"}],
    )
    _content, calls = _run_summarize([r], judge_verdict=SUPPORTED)
    judge_user = next(m for m in calls["judge"][0] if m["role"] == "user")["content"]
    assert "wrote the parser" in judge_user
    assert "SyntaxError: unexpected EOF" in judge_user
    assert "out/parser.py" in judge_user


# --- judge_false_completion(): the helper in isolation -----------------------

def _judge(results, flags, verdict=SUPPORTED, record=None):
    def fake_chat(client, messages, **kw):
        if record is not None:
            record.append(kw.get("model"))
        return verdict, 0, 0

    with patch.object(summarizer, "chat", fake_chat):
        return summarizer.judge_false_completion(
            results, "task", flags, client=MagicMock()
        )


def test_judge_helper_flags_unsupported_uses_classifier_model():
    """The helper flags an unsupported COMPLETED result and the ONE call it makes
    targets the cheap classifier model."""
    models = []
    flags = _judge([_completed("1", exit_code=0)], [], verdict=UNSUPPORTED, record=models)
    assert [sid for sid, _ in flags] == ["1"]
    assert models == [summarizer.CLASSIFIER_MODEL]


def test_judge_helper_skips_already_flagged():
    """Cost guard at the helper level: a deterministically-flagged result is
    never passed to the model."""
    results = [_completed("1", exit_code=1)]
    det = summarizer.detect_false_completion(results)
    models = []
    flags = _judge(results, det, verdict=UNSUPPORTED, record=models)
    assert models == []
    assert flags == []


def test_judge_helper_supported_returns_no_flags():
    results = [_completed("1", exit_code=0)]
    assert _judge(results, [], verdict=SUPPORTED) == []


def test_judge_call_failure_does_not_flag_or_raise():
    """A failed/garbled judge call defaults to SUPPORTED — a flaky judge must
    never inject a spurious caveat (false-positive flags degrade the answer)."""
    def boom_chat(client, messages, **kw):
        raise RuntimeError("classifier exploded")

    with patch.object(summarizer, "chat", boom_chat):
        flags = summarizer.judge_false_completion(
            [_completed("1", exit_code=0)], "task", [], client=MagicMock()
        )
    assert flags == []
