"""Tests for agent awareness features: plan context, pane env, peek, budget."""
from models import Subtask, Plan
from executor import _build_plan_context, _capture_pane_env, parse_command


def test_plan_context_shows_role():
    plan = Plan(task="analyze sales data", subtasks=[
        Subtask(id="1", description="extract data", pane="shell", mode="script"),
        Subtask(id="2", description="fetch reference", pane="browser", mode="interactive"),
        Subtask(id="3", description="merge and report", pane="shell", mode="script", depends_on=["1", "2"]),
    ])
    ctx = _build_plan_context(plan, plan.subtasks[0])
    assert "subtask 1 of 3" in ctx
    assert "analyze sales" in ctx


def test_plan_context_shows_downstream():
    plan = Plan(task="test", subtasks=[
        Subtask(id="1", description="extract", pane="shell"),
        Subtask(id="2", description="report", pane="shell", depends_on=["1"]),
    ])
    ctx = _build_plan_context(plan, plan.subtasks[0])
    assert "Downstream" in ctx or "needs your output" in ctx


def test_plan_context_shows_parallel():
    plan = Plan(task="test", subtasks=[
        Subtask(id="1", description="task a", pane="shell"),
        Subtask(id="2", description="task b", pane="browser"),
    ])
    ctx = _build_plan_context(plan, plan.subtasks[0])
    assert "Parallel" in ctx or "2:browser" in ctx


def test_parse_peek_command():
    text = '<cmd type="peek" pane="browser">check what the browser shows</cmd>'
    cmd = parse_command(text)
    assert cmd["type"] == "peek"
    assert cmd["pane"] == "browser"


def test_parse_peek_without_pane():
    text = '<cmd type="peek">browser</cmd>'
    cmd = parse_command(text)
    assert cmd["type"] == "peek"
    assert cmd["value"] == "browser"
