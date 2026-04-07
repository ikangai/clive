"""Tests for plan-to-script compiler (collapsing sequential DAGs)."""
from models import Subtask, Plan
from executor import _try_collapse_plan


def test_collapse_linear_script_chain():
    plan = Plan(task="data pipeline", subtasks=[
        Subtask(id="1", description="extract data", pane="shell", mode="script"),
        Subtask(id="2", description="filter rows", pane="shell", mode="script", depends_on=["1"]),
        Subtask(id="3", description="generate report", pane="shell", mode="script", depends_on=["2"]),
    ])
    collapsed = _try_collapse_plan(plan)
    assert len(collapsed.subtasks) == 1
    assert "Step 1" in collapsed.subtasks[0].description
    assert "Step 3" in collapsed.subtasks[0].description
    assert collapsed.subtasks[0].mode == "script"


def test_no_collapse_mixed_modes():
    plan = Plan(task="research", subtasks=[
        Subtask(id="1", description="fetch page", pane="shell", mode="interactive"),
        Subtask(id="2", description="analyze", pane="shell", mode="script", depends_on=["1"]),
    ])
    collapsed = _try_collapse_plan(plan)
    assert len(collapsed.subtasks) == 2  # not collapsed


def test_no_collapse_multi_pane():
    plan = Plan(task="parallel work", subtasks=[
        Subtask(id="1", description="shell work", pane="shell", mode="script"),
        Subtask(id="2", description="browser work", pane="browser", mode="script"),
    ])
    collapsed = _try_collapse_plan(plan)
    assert len(collapsed.subtasks) == 2  # different panes


def test_no_collapse_parallel_deps():
    plan = Plan(task="fork join", subtasks=[
        Subtask(id="1", description="a", pane="shell", mode="script"),
        Subtask(id="2", description="b", pane="shell", mode="script"),
        Subtask(id="3", description="merge", pane="shell", mode="script", depends_on=["1", "2"]),
    ])
    collapsed = _try_collapse_plan(plan)
    assert len(collapsed.subtasks) == 3  # not a linear chain


def test_single_subtask_unchanged():
    plan = Plan(task="simple", subtasks=[
        Subtask(id="1", description="do it", pane="shell", mode="script"),
    ])
    collapsed = _try_collapse_plan(plan)
    assert len(collapsed.subtasks) == 1
    assert collapsed.subtasks[0].id == "1"  # original, not "compiled"
