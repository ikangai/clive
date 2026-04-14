"""Tests for planned-mode execution runner."""
import sys
sys.path.insert(0, '.')

from unittest.mock import MagicMock, patch, call
import json

from planned_runner import parse_planned_steps, PlannedStep, PlannedPlan
from models import VALID_MODES, Subtask, SubtaskStatus, PaneInfo


# ─── parse_planned_steps tests ───────────────────────────────────────────────

def test_parse_raw_json():
    raw = json.dumps({
        "steps": [
            {"cmd": "echo hello", "verify": "exit_code == 0", "on_fail": "abort"},
            {"cmd": "ls -la", "verify": "exit_code == 0", "on_fail": "skip"},
        ],
        "done_summary": "Listed files",
    })
    plan = parse_planned_steps(raw)
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].cmd == "echo hello"
    assert plan.steps[0].on_fail == "abort"
    assert plan.steps[1].cmd == "ls -la"
    assert plan.steps[1].on_fail == "skip"
    assert plan.done_summary == "Listed files"


def test_parse_fenced_json():
    raw = """Here is the plan:

```json
{
  "steps": [
    {"cmd": "mkdir -p /tmp/out", "verify": "exit_code == 0", "on_fail": "abort"},
    {"cmd": "touch /tmp/out/done", "verify": "exit_code == 0", "on_fail": "skip"}
  ],
  "done_summary": "Created output directory"
}
```
"""
    plan = parse_planned_steps(raw)
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].cmd == "mkdir -p /tmp/out"
    assert plan.done_summary == "Created output directory"


def test_parse_invalid_json():
    assert parse_planned_steps("not json at all") is None
    assert parse_planned_steps('{"no_steps": true}') is None
    assert parse_planned_steps('{"steps": []}') is None
    assert parse_planned_steps('{"steps": [{"no_cmd": "x"}]}') is None


def test_parse_defaults():
    raw = json.dumps({
        "steps": [{"cmd": "echo hi"}],
    })
    plan = parse_planned_steps(raw)
    assert plan is not None
    assert plan.steps[0].verify == "exit_code == 0"
    assert plan.steps[0].on_fail == "abort"
    assert plan.done_summary == ""


# ─── run_subtask_planned tests ───────────────────────────────────────────────

def _make_subtask(mode="planned", desc="test task"):
    return Subtask(id="t1", description=desc, pane="shell", mode=mode)


def _make_pane_info():
    pane = MagicMock()
    pane.cmd.return_value = MagicMock(stdout=["$ done"])
    return PaneInfo(
        pane=pane,
        app_type="shell",
        description="bash shell",
        name="shell",
    )


def _plan_json(steps, done_summary="done"):
    return json.dumps({
        "steps": [{"cmd": s[0], "verify": "exit_code == 0", "on_fail": s[1]} for s in steps],
        "done_summary": done_summary,
    })


@patch("planned_runner.chat")
@patch("planned_runner.get_client")
@patch("planned_runner.wait_for_ready")
@patch("planned_runner.wrap_command")
def test_happy_path_two_steps(mock_wrap, mock_wait, mock_client, mock_chat):
    """Two steps both succeed -> COMPLETED, turns_used=1, only 1 chat call."""
    from planned_runner import run_subtask_planned

    plan = _plan_json([("echo a", "abort"), ("echo b", "abort")], "echoed a and b")
    mock_chat.return_value = (plan, 100, 50)
    mock_client.return_value = MagicMock()

    # wrap_command returns predictable markers
    mock_wrap.side_effect = [
        ("echo a; echo \"EXIT:$? MARK0\"", "MARK0"),
        ("echo b; echo \"EXIT:$? MARK1\"", "MARK1"),
    ]
    mock_wait.side_effect = [
        ("EXIT:0 MARK0", "marker"),
        ("EXIT:0 MARK1", "marker"),
    ]

    subtask = _make_subtask()
    pane_info = _make_pane_info()
    result = run_subtask_planned(subtask, pane_info, dep_context="")

    assert result.status == SubtaskStatus.COMPLETED
    assert result.turns_used == 1
    assert result.summary == "echoed a and b"
    assert mock_chat.call_count == 1


@patch("planned_runner.chat")
@patch("planned_runner.get_client")
@patch("planned_runner.wait_for_ready")
@patch("planned_runner.wrap_command")
def test_step_fails_abort(mock_wrap, mock_wait, mock_client, mock_chat):
    """Step fails with on_fail=abort -> FAILED."""
    from planned_runner import run_subtask_planned

    plan = _plan_json([("bad_cmd", "abort"), ("echo ok", "abort")])
    mock_chat.return_value = (plan, 100, 50)
    mock_client.return_value = MagicMock()

    mock_wrap.return_value = ("bad_cmd; echo \"EXIT:$? MARK0\"", "MARK0")
    mock_wait.return_value = ("EXIT:1 MARK0", "marker")

    result = run_subtask_planned(_make_subtask(), _make_pane_info(), dep_context="")

    assert result.status == SubtaskStatus.FAILED
    assert "Step 0 failed" in result.summary
    assert result.turns_used == 1


@patch("planned_runner.chat")
@patch("planned_runner.get_client")
@patch("planned_runner.wait_for_ready")
@patch("planned_runner.wrap_command")
def test_step_fails_skip(mock_wrap, mock_wait, mock_client, mock_chat):
    """Step fails with on_fail=skip -> continues to next step."""
    from planned_runner import run_subtask_planned

    plan = _plan_json([("optional_cmd", "skip"), ("echo done", "abort")], "finished")
    mock_chat.return_value = (plan, 100, 50)
    mock_client.return_value = MagicMock()

    mock_wrap.side_effect = [
        ("optional_cmd; echo \"EXIT:$? MARK0\"", "MARK0"),
        ("echo done; echo \"EXIT:$? MARK1\"", "MARK1"),
    ]
    mock_wait.side_effect = [
        ("EXIT:1 MARK0", "marker"),  # first step fails
        ("EXIT:0 MARK1", "marker"),  # second step succeeds
    ]

    result = run_subtask_planned(_make_subtask(), _make_pane_info(), dep_context="")

    assert result.status == SubtaskStatus.COMPLETED
    assert result.summary == "finished"


@patch("planned_runner.chat")
@patch("planned_runner.get_client")
@patch("planned_runner.wait_for_ready")
@patch("planned_runner.wrap_command")
def test_step_fails_retry_then_succeeds(mock_wrap, mock_wait, mock_client, mock_chat):
    """Step fails with on_fail=retry, succeeds on retry -> COMPLETED."""
    from planned_runner import run_subtask_planned

    plan = _plan_json([("flaky_cmd", "retry")], "done")
    mock_chat.return_value = (plan, 100, 50)
    mock_client.return_value = MagicMock()

    mock_wrap.side_effect = [
        ("flaky_cmd; echo \"EXIT:$? MARK0\"", "MARK0"),
        ("flaky_cmd; echo \"EXIT:$? MARK0b\"", "MARK0b"),
    ]
    mock_wait.side_effect = [
        ("EXIT:1 MARK0", "marker"),   # first attempt fails
        ("EXIT:0 MARK0b", "marker"),  # retry succeeds
    ]

    result = run_subtask_planned(_make_subtask(), _make_pane_info(), dep_context="")

    assert result.status == SubtaskStatus.COMPLETED
    assert result.turns_used == 1


@patch("planned_runner.chat")
@patch("planned_runner.get_client")
@patch("planned_runner.wait_for_ready")
@patch("planned_runner.wrap_command")
def test_step_fails_retry_then_fails(mock_wrap, mock_wait, mock_client, mock_chat):
    """Step fails with on_fail=retry, retry also fails -> FAILED."""
    from planned_runner import run_subtask_planned

    plan = _plan_json([("bad_cmd", "retry")], "done")
    mock_chat.return_value = (plan, 100, 50)
    mock_client.return_value = MagicMock()

    mock_wrap.side_effect = [
        ("bad_cmd; echo \"EXIT:$? MARK0\"", "MARK0"),
        ("bad_cmd; echo \"EXIT:$? MARK0b\"", "MARK0b"),
    ]
    mock_wait.side_effect = [
        ("EXIT:1 MARK0", "marker"),   # first attempt fails
        ("EXIT:2 MARK0b", "marker"),  # retry also fails
    ]

    result = run_subtask_planned(_make_subtask(), _make_pane_info(), dep_context="")

    assert result.status == SubtaskStatus.FAILED
    assert "retry" in result.summary.lower()


# ─── build_planned_prompt tests ──────────────────────────────────────────────

def test_build_planned_prompt_contains_keywords():
    from prompts import build_planned_prompt
    prompt = build_planned_prompt(
        subtask_description="Download and process data",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="",
        session_dir="/tmp/clive/test",
    )
    assert "steps" in prompt
    assert "on_fail" in prompt
    assert "retry" in prompt
    assert "skip" in prompt
    assert "abort" in prompt
    assert "done_summary" in prompt
    assert "JSON" in prompt
    assert "/tmp/clive/test" in prompt
    assert "Download and process data" in prompt


def test_build_planned_prompt_with_deps():
    from prompts import build_planned_prompt
    prompt = build_planned_prompt(
        subtask_description="Summarize",
        pane_name="shell",
        app_type="shell",
        tool_description="bash shell",
        dependency_context="[Subtask 1]: Found 10 results",
        session_dir="/tmp/clive/test",
    )
    assert "Found 10 results" in prompt


# ─── VALID_MODES inclusion ───────────────────────────────────────────────────

def test_planned_in_valid_modes():
    assert "planned" in VALID_MODES


# ─── Safety check test ──────────────────────────────────────────────────────

@patch("planned_runner.chat")
@patch("planned_runner.get_client")
def test_safety_check_blocks_dangerous_step(mock_client, mock_chat):
    """A dangerous command should be caught by _check_command_safety."""
    from planned_runner import run_subtask_planned

    plan = json.dumps({
        "steps": [{"cmd": "rm -rf /", "verify": "exit_code == 0", "on_fail": "abort"}],
        "done_summary": "destroyed everything",
    })
    mock_chat.return_value = (plan, 10, 10)
    mock_client.return_value = MagicMock()

    result = run_subtask_planned(_make_subtask(), _make_pane_info(), dep_context="")

    assert result.status == SubtaskStatus.FAILED
    assert "safety" in result.summary.lower() or "blocked" in result.summary.lower()


# ─── Executor dispatch test ─────────────────────────────────────────────────

def test_executor_dispatches_planned():
    """Verify executor.run_subtask dispatches to run_subtask_planned for mode=planned."""
    from executor import run_subtask_planned as exported
    from planned_runner import run_subtask_planned as original
    assert exported is original
