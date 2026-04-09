# tests/test_conversational_loop.py
"""Tests for the conversational protocol handler."""

from server.conversational import ConversationalHandler


def test_handler_processes_single_task():
    """Handler must process a single task and emit protocol lines."""
    output_lines = []
    def mock_run(task, **kwargs):
        return {"summary": f"Ran: {task}"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("echo hello")

    # Must emit TURN: thinking, then TURN: done
    assert any("TURN: thinking" in line for line in output_lines)
    assert any("TURN: done" in line for line in output_lines)


def test_handler_emits_done_with_result():
    """Handler must emit DONE: with JSON result."""
    output_lines = []
    def mock_run(task, **kwargs):
        return {"summary": "done"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("test task")

    done_lines = [l for l in output_lines if l.startswith("DONE:")]
    assert len(done_lines) == 1


def test_handler_emits_failed_on_error():
    """Handler must emit TURN: failed on exception."""
    output_lines = []
    def mock_run(task, **kwargs):
        raise RuntimeError("boom")

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("bad task")

    assert any("TURN: failed" in line for line in output_lines)


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
    done_lines = [l for l in output_lines if l.startswith("DONE:")]
    assert len(done_lines) == 2


def test_handler_emits_context():
    """Handler must emit CONTEXT: with JSON data."""
    output_lines = []
    def mock_run(task, **kwargs):
        return {"summary": "ok"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.handle_task("test")

    ctx_lines = [l for l in output_lines if l.startswith("CONTEXT:")]
    assert len(ctx_lines) >= 1


def test_handler_ask_question():
    """Handler must be able to emit QUESTION: lines."""
    output_lines = []
    def mock_run(task, **kwargs):
        return {"summary": "ok"}

    handler = ConversationalHandler(run_fn=mock_run, emit_fn=output_lines.append)
    handler.ask_question("What format?")

    assert any("TURN: waiting" in line for line in output_lines)
    assert any("QUESTION: What format?" in line for line in output_lines)
