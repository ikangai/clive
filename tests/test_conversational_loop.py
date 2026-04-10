# tests/test_conversational_loop.py
"""Tests for the conversational protocol handler (framed)."""

from protocol import decode_all
from server.conversational import ConversationalHandler


def _frames(lines: list[str]):
    return decode_all("\n".join(lines))


def test_handler_processes_single_task():
    """Handler must process a single task and emit turn frames."""
    output_lines = []
    def mock_run(task, **kwargs):
        return {"summary": f"Ran: {task}"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("echo hello")

    frames = _frames(output_lines)
    turn_states = [f.payload.get("state") for f in frames if f.kind == "turn"]
    assert "thinking" in turn_states
    assert "done" in turn_states


def test_handler_emits_context_with_result():
    """Handler must emit a context frame containing the result."""
    output_lines = []
    def mock_run(task, **kwargs):
        return {"summary": "done"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("test task")

    ctx_frames = [f for f in _frames(output_lines) if f.kind == "context"]
    assert len(ctx_frames) == 1
    assert ctx_frames[0].payload.get("result") == {"summary": "done"}


def test_handler_emits_failed_on_error():
    """Handler must emit turn=failed on exception."""
    output_lines = []
    def mock_run(task, **kwargs):
        raise RuntimeError("boom")

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("bad task")

    frames = _frames(output_lines)
    turn_states = [f.payload.get("state") for f in frames if f.kind == "turn"]
    assert "failed" in turn_states
    ctx_frames = [f for f in frames if f.kind == "context"]
    assert any("boom" in str(f.payload.get("error", "")) for f in ctx_frames)


def test_handler_processes_multiple_tasks():
    """Handler must support sequential tasks."""
    output_lines = []
    call_count = [0]
    def mock_run(task, **kwargs):
        call_count[0] += 1
        return {"summary": f"result {call_count[0]}"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("task 1")
    handler.handle_task("task 2")

    assert call_count[0] == 2
    frames = _frames(output_lines)
    done_count = sum(1 for f in frames if f.kind == "turn" and f.payload.get("state") == "done")
    assert done_count == 2


def test_handler_emits_context():
    """Handler must emit at least one context frame per task."""
    output_lines = []
    def mock_run(task, **kwargs):
        return {"summary": "ok"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("test")

    ctx_frames = [f for f in _frames(output_lines) if f.kind == "context"]
    assert len(ctx_frames) >= 1


def test_handler_ask_question():
    """Handler must be able to emit question frames."""
    output_lines = []
    def mock_run(task, **kwargs):
        return {"summary": "ok"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.ask_question("What format?")

    frames = _frames(output_lines)
    turn_states = [f.payload.get("state") for f in frames if f.kind == "turn"]
    assert "waiting" in turn_states
    questions = [f.payload.get("text") for f in frames if f.kind == "question"]
    assert "What format?" in questions
