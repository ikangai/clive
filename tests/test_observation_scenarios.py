"""Tests for observation latency benchmark scenarios."""
from evals.observation.scenarios import SCENARIOS, Scenario


def test_scenarios_has_all_six():
    assert len(SCENARIOS) == 6
    assert {s.id for s in SCENARIOS} == {
        "error_scroll", "password_prompt", "confirm_prompt",
        "spinner_ok", "spinner_fail", "color_only",
    }


def test_each_scenario_has_shell_command():
    for s in SCENARIOS:
        assert isinstance(s, Scenario)
        assert s.shell_command  # non-empty
        assert s.expected_l2_kinds  # non-empty tuple
        assert s.target_description  # non-empty for reporting


def test_color_only_is_marked_baseline_blind():
    # Scenario 6 baseline cannot detect — harness uses this flag to expect missed=True
    color_only = next(s for s in SCENARIOS if s.id == "color_only")
    assert color_only.baseline_blind is True

    error_scroll = next(s for s in SCENARIOS if s.id == "error_scroll")
    assert error_scroll.baseline_blind is False
