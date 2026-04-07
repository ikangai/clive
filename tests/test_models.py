"""Tests for data models."""
from models import Subtask, Plan, VALID_MODES


def test_subtask_default_mode():
    s = Subtask(id="1", description="test", pane="shell")
    assert s.mode == "interactive"


def test_subtask_script_mode():
    s = Subtask(id="1", description="test", pane="shell", mode="script")
    assert s.mode == "script"


def test_subtask_streaming_mode():
    s = Subtask(id="1", description="test", pane="shell", mode="streaming")
    assert s.mode == "streaming"


def test_subtask_invalid_mode_defaults_to_interactive():
    s = Subtask(id="1", description="test", pane="shell", mode="garbage")
    assert s.mode == "interactive"


def test_subtask_retried_field():
    s = Subtask(id="1", description="test", pane="shell")
    assert s._retried is False
    s._retried = True
    assert s._retried is True


def test_valid_modes_constant():
    assert "script" in VALID_MODES
    assert "interactive" in VALID_MODES
    assert "streaming" in VALID_MODES


def test_plan_validates_with_mode():
    plan = Plan(task="test")
    plan.subtasks.append(Subtask(id="1", description="t", pane="shell", mode="script"))
    plan.subtasks.append(Subtask(id="2", description="t", pane="shell", mode="interactive", depends_on=["1"]))
    errors = plan.validate(valid_panes={"shell"})
    assert errors == []
