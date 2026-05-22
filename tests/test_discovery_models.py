"""Tests for discovery.models — data classes only, no LLM or pane interaction."""
from discovery.models import ExplorationResult, ProbeOutcome


def test_probe_outcome_success_when_exit_zero():
    p = ProbeOutcome(command="echo --help", exit_code=0, screen="Usage: echo")
    assert p.success is True


def test_probe_outcome_failure_on_nonzero():
    p = ProbeOutcome(command="echo --foo", exit_code=2, screen="bad arg")
    assert p.success is False


def test_probe_outcome_failure_on_none_exit():
    # exit_code=None means "blocked by safety check or never executed".
    p = ProbeOutcome(command="rm -rf /", exit_code=None, screen="[Blocked]")
    assert p.success is False


def test_exploration_result_aggregates_probes():
    r = ExplorationResult(tool_name="echo", probes=[
        ProbeOutcome(command="echo --help", exit_code=0, screen="usage..."),
        ProbeOutcome(command="echo --version", exit_code=2, screen="bad"),
    ])
    assert r.tool_name == "echo"
    assert r.success_count == 1
    assert r.failure_count == 1
    assert len(r.probes) == 2
