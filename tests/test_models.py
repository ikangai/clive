"""Tests for data models."""
from models import Subtask, Plan


def test_subtask_default_mode():
    s = Subtask(id="1", description="test", pane="shell")
    assert s.mode == "interactive"


def test_subtask_script_mode():
    s = Subtask(id="1", description="test", pane="shell", mode="script")
    assert s.mode == "script"


def test_plan_validates_with_mode():
    plan = Plan(task="test")
    plan.subtasks.append(Subtask(id="1", description="t", pane="shell", mode="script"))
    plan.subtasks.append(Subtask(id="2", description="t", pane="shell", mode="interactive", depends_on=["1"]))
    errors = plan.validate(valid_panes={"shell"})
    assert errors == []
