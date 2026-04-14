"""Integration tests for planned mode — planner awareness and end-to-end."""
import sys
sys.path.insert(0, '.')

import json
from unittest.mock import patch, MagicMock

from models import Subtask, Plan, SubtaskStatus, PaneInfo
from prompts import build_planner_prompt


def test_planned_mode_in_planner_prompt():
    """Planner prompt should mention 'planned' mode."""
    prompt = build_planner_prompt("shell [shell] — Bash\n")
    assert "planned" in prompt
    assert "multi-step" in prompt.lower() or "Multi-step" in prompt


def test_planned_mode_valid_in_plan():
    """A plan with planned mode subtask should validate successfully."""
    subtask = Subtask(id="1", description="fetch and process", pane="shell", mode="planned")
    plan = Plan(task="test", subtasks=[subtask])
    errors = plan.validate(valid_panes={"shell"})
    assert not errors


def test_planned_mode_turns_count():
    """Planned mode should report turns_used=1 on happy path."""
    from planned_runner import run_subtask_planned, _execute_step

    pane = MagicMock()
    pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
    pane_info = PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")
    subtask = Subtask(id="1", description="do stuff", pane="shell", mode="planned", max_turns=5)

    plan_response = json.dumps({
        "steps": [{"cmd": "echo hello", "verify": "exit_code == 0", "on_fail": "abort"}],
        "done_summary": "Said hello"
    })

    with patch("planned_runner.chat", return_value=(plan_response, 100, 50)), \
         patch("planned_runner._execute_step", return_value=(0, "hello\n[AGENT_READY] $ ")), \
         patch("planned_runner.capture_pane", return_value="[AGENT_READY] $ "):
        result = run_subtask_planned(subtask=subtask, pane_info=pane_info, dep_context="")

    assert result.status == SubtaskStatus.COMPLETED
    assert result.turns_used == 1
    assert result.prompt_tokens == 100


def test_all_modes_in_planner_prompt():
    """Planner prompt should mention all valid modes."""
    prompt = build_planner_prompt("shell [shell] — Bash\n")
    for mode in ["script", "planned", "interactive", "streaming"]:
        assert f'"{mode}"' in prompt, f"Mode {mode} missing from planner prompt"
