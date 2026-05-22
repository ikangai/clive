"""Regression tests: planner JSON 'tools' field is forwarded to Subtask.tools.

The Task 7 contract: when the LLM emits "tools": [...] in a subtask, the
constructed Subtask object must carry that list on .tools. Previously the
planner's Subtask constructor dropped the field on the floor, so the contract
was non-functional end-to-end.
"""
import json
from unittest.mock import patch

import planner
from models import PaneInfo


def _panes() -> dict[str, PaneInfo]:
    """Minimal pane registry for plan validation.

    Plan.validate only reads dict keys (pane names), so the PaneInfo value
    can carry a None tmux pane handle here.
    """
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


def _run_planner(plan_json: dict):
    """Invoke create_plan with a hand-crafted JSON payload as the LLM output."""
    payload = json.dumps(plan_json)
    with patch("planner.chat", return_value=(payload, 10, 10)), \
         patch("planner.get_client", return_value=object()):
        return planner.create_plan(
            task="dummy task",
            panes=_panes(),
            tool_status=_tool_status(),
            tools_summary="shell [shell] — Bash\n",
        )


def test_planner_forwards_tools_list_to_subtask():
    """JSON with "tools": ["jq", "rg"] produces Subtask(tools=["jq", "rg"])."""
    plan_json = {
        "subtasks": [
            {
                "id": "1",
                "description": "extract fields",
                "pane": "shell",
                "mode": "script",
                "tools": ["jq", "rg"],
                "depends_on": [],
            }
        ]
    }
    plan = _run_planner(plan_json)
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].tools == ["jq", "rg"]


def test_planner_missing_tools_defaults_to_empty_list():
    """When the JSON omits "tools", the Subtask.tools defaults to []."""
    plan_json = {
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
    plan = _run_planner(plan_json)
    assert plan.subtasks[0].tools == []


def test_planner_null_tools_normalizes_to_empty_list():
    """A null/None value for tools must be coerced to [], not propagated."""
    plan_json = {
        "subtasks": [
            {
                "id": "1",
                "description": "do a thing",
                "pane": "shell",
                "mode": "script",
                "tools": None,
                "depends_on": [],
            }
        ]
    }
    plan = _run_planner(plan_json)
    assert plan.subtasks[0].tools == []


def test_planner_string_tools_does_not_explode_into_chars():
    """A string value (planner mistake) must NOT become ['y','t','-',...]."""
    plan_json = {
        "subtasks": [
            {
                "id": "1",
                "description": "fetch video",
                "pane": "shell",
                "mode": "script",
                "tools": "yt-dlp",
                "depends_on": [],
            }
        ]
    }
    plan = _run_planner(plan_json)
    # Defensive guard rejects non-list values rather than calling list("yt-dlp")
    assert plan.subtasks[0].tools == []


def test_planner_coerces_non_string_tool_elements():
    """Non-string elements in a tools list get str()'d, not dropped."""
    plan_json = {
        "subtasks": [
            {
                "id": "1",
                "description": "x",
                "pane": "shell",
                "mode": "script",
                "tools": ["jq", 42],
                "depends_on": [],
            }
        ]
    }
    plan = _run_planner(plan_json)
    assert plan.subtasks[0].tools == ["jq", "42"]
