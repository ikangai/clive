"""Self-repair tests: the planner retries malformed/invalid plans with feedback.

The contract (task-99b11b74): create_plan's retry loop must not abort the whole
task the moment the LLM emits unparseable JSON or a plan that fails
plan.validate() (unknown pane ref, cyclic depends_on, etc). Instead it appends
the specific error to the conversation and re-asks the planner, up to a bounded
number of attempts — the standard structured-output validate-and-correct loop.
Only after exhausting the budget does it raise.
"""
import json
from unittest.mock import patch, MagicMock

import planner
from models import PaneInfo


def _panes() -> dict[str, PaneInfo]:
    """Minimal pane registry; Plan.validate only reads the dict keys."""
    return {
        "shell": PaneInfo(
            pane=None,  # type: ignore[arg-type]  -- validate only reads keys
            app_type="shell",
            description="Bash",
            name="shell",
        )
    }


def _tool_status() -> dict[str, dict]:
    return {"shell": {"status": "ready", "app_type": "shell", "description": "Bash"}}


def _valid_plan_json() -> str:
    return json.dumps(
        {
            "subtasks": [
                {
                    "id": "1",
                    "description": "do a thing",
                    "pane": "shell",
                    "mode": "interactive",
                    "depends_on": [],
                }
            ]
        }
    )


def _plan_with_unknown_pane() -> str:
    return json.dumps(
        {
            "subtasks": [
                {
                    "id": "1",
                    "description": "do a thing",
                    "pane": "ghost",  # not in the pane registry
                    "mode": "interactive",
                    "depends_on": [],
                }
            ]
        }
    )


def _run_with_responses(responses):
    """Patch planner.chat to yield (content, pt, ct) for each response in turn.

    Returns (plan_or_exc, mock_chat) so callers can inspect call history.
    """
    chat_mock = MagicMock(side_effect=[(r, 10, 10) for r in responses])
    with patch("planner.chat", chat_mock), \
         patch("planner.get_client", return_value=object()):
        plan = planner.create_plan(
            task="dummy task",
            panes=_panes(),
            tool_status=_tool_status(),
            tools_summary="shell [shell] — Bash\n",
        )
    return plan, chat_mock


def test_malformed_json_then_valid_yields_plan():
    """Garbage on attempt 1, valid JSON on attempt 2 → a valid Plan."""
    plan, chat_mock = _run_with_responses(["this is not json at all", _valid_plan_json()])
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].pane == "shell"
    assert chat_mock.call_count == 2


def test_unknown_pane_triggers_feedback_retry_not_crash():
    """A plan referencing a nonexistent pane must re-ask, not crash immediately."""
    plan, chat_mock = _run_with_responses([_plan_with_unknown_pane(), _valid_plan_json()])
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].pane == "shell"
    assert chat_mock.call_count == 2

    # The corrective second call must carry the specific validation error so the
    # planner can fix it (validate-and-correct, not a blind re-ask).
    second_messages = chat_mock.call_args_list[1].args[1]
    feedback = " ".join(m["content"] for m in second_messages)
    assert "ghost" in feedback  # the offending pane name surfaced as feedback


def test_cyclic_depends_on_is_repairable():
    """A cyclic depends_on (caught by validate) is fed back and repaired."""
    cyclic = json.dumps(
        {
            "subtasks": [
                {"id": "1", "description": "a", "pane": "shell", "depends_on": ["2"]},
                {"id": "2", "description": "b", "pane": "shell", "depends_on": ["1"]},
            ]
        }
    )
    plan, chat_mock = _run_with_responses([cyclic, _valid_plan_json()])
    assert len(plan.subtasks) == 1
    assert chat_mock.call_count == 2


def test_persistent_invalid_plan_raises_after_bounded_retries():
    """If every attempt is invalid, give up with ValueError — but bounded."""
    bad = [_plan_with_unknown_pane()] * 10  # always invalid
    chat_mock = MagicMock(side_effect=[(r, 10, 10) for r in bad])
    with patch("planner.chat", chat_mock), \
         patch("planner.get_client", return_value=object()):
        try:
            planner.create_plan(
                task="dummy task",
                panes=_panes(),
                tool_status=_tool_status(),
                tools_summary="shell [shell] — Bash\n",
            )
            raised = None
        except ValueError as e:
            raised = e
    assert raised is not None
    # Bounded: it must stop retrying, not loop forever / consume all 10 responses.
    assert 1 < chat_mock.call_count <= 5


def test_valid_plan_first_try_calls_chat_once():
    """The happy path is unchanged: one chat call, no retries."""
    plan, chat_mock = _run_with_responses([_valid_plan_json()])
    assert len(plan.subtasks) == 1
    assert chat_mock.call_count == 1
