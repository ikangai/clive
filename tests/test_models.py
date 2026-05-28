"""Tests for data models."""
import pytest

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


def test_subtask_id_rejects_shell_metachars():
    """Audit H2 (2026-05-27): id is interpolated into the shell wrapper string
    f'{cmd}; echo "EXIT:$? ___DONE_{id}_{nonce}___"' — a planner-LLM-controlled
    id containing shell metachars bypasses every runner's safety gate (which
    inspects cmd, not the wrapper). Validation must reject at construction.
    """
    with pytest.raises(ValueError):
        Subtask(id='x"; rm -rf ~; echo "y', description="test", pane="shell")


@pytest.mark.parametrize("bad_id", [
    "",                          # empty
    "a" * 41,                    # > 40 chars
    "with space",                # whitespace
    "with\nnewline",             # newline
    'x"; rm -rf ~',              # quote + semicolon
    "x'; rm -rf ~",              # single quote + semicolon
    "x`whoami`",                 # backticks
    "x$(whoami)",                # command substitution
    "x$IFS$9",                   # IFS expansion
    "x&&y",                      # AND chain
    "x||y",                      # OR chain
    "x|y",                       # pipe
    "x;y",                       # statement separator
    "x>y",                       # redirect
    "x<y",                       # redirect
    "x(y",                       # subshell
    "x\\y",                      # backslash
    "x*y",                       # glob
    "x?y",                       # glob
    "x[y",                       # glob bracket
    "x{y}",                      # brace expansion
    "x#y",                       # comment leader
    "../escape",                 # path traversal shape
    ".hidden",                   # leading dot (no use case; ban as defense in depth)
    "x.y",                       # dot (no use case; matches gh#41 _check_tool_name policy)
])
def test_subtask_id_rejects_unsafe_shapes(bad_id):
    """Regression guard: every id shape that could either inject shell text or
    bypass the safety gate must be rejected at Subtask construction time.
    """
    with pytest.raises(ValueError):
        Subtask(id=bad_id, description="test", pane="shell")


@pytest.mark.parametrize("good_id", [
    "1",                         # planner default (numeric)
    "999",                       # multi-digit numeric
    "step-1",                    # hyphenated
    "subtask_2",                 # underscored
    "compiled",                  # dag_scheduler's collapsed-script id
    "explore-foo-deadbeef",      # discovery exploration id format
    "a" * 40,                    # boundary: exactly 40 chars
    "ABC",                       # uppercase allowed
    "MixedCase_123-abc",         # mixed
])
def test_subtask_id_accepts_safe_shapes(good_id):
    """Regression guard: every id shape actually used by in-tree callers
    (planner, router, dag_scheduler, discovery, cli_modes) must continue to
    construct cleanly.
    """
    s = Subtask(id=good_id, description="test", pane="shell")
    assert s.id == good_id


def test_subtask_id_rejects_non_string():
    """Non-string ids (e.g. raw int from a JSON parser that skipped str())
    must be rejected explicitly rather than match-failing the regex with a
    cryptic TypeError.
    """
    with pytest.raises(ValueError):
        Subtask(id=1, description="test", pane="shell")  # type: ignore[arg-type]


def test_subtask_id_gate_fires_on_planner_call_path():
    """Integration: mirrors planner.py:60-81's `Subtask(id=str(s["id"]), ...)`
    construction with a planner-LLM payload carrying an injection-shaped id.
    Catches the class of regression where the gate is correct in isolation but
    a future refactor swaps `Subtask(...)` for a dict or a `**kwargs` builder
    that bypasses __post_init__. The lesson from the 2026-05-27 audit was that
    a gate uncalled on the production path is no gate at all.
    """
    planner_response = {"id": 'x"; rm -rf ~; echo "y', "pane": "shell"}
    with pytest.raises(ValueError):
        Subtask(
            id=str(planner_response["id"]),
            description="test",
            pane=planner_response["pane"],
        )


def test_plan_validates_with_mode():
    plan = Plan(task="test")
    plan.subtasks.append(Subtask(id="1", description="t", pane="shell", mode="script"))
    plan.subtasks.append(Subtask(id="2", description="t", pane="shell", mode="interactive", depends_on=["1"]))
    errors = plan.validate(valid_panes={"shell"})
    assert errors == []
