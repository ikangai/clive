"""Tests for the Layer 5 tool-discovery eval support (gh#40).

Covers the pure-python harness pieces: discovery context building,
discovery-criteria checking against pane scrollback, disabled-tool PATH
shims, the ToolEvalResult metrics extension, and the layer5 tasks.json
schema. The actual LLM-driven eval runs are exercised by run_eval.py,
not by this suite.
"""
import json
import os
import stat
import subprocess

import pytest

from evals.harness.discovery_eval import (
    PROMPT_MARKER,
    build_discovery_context,
    check_discovery_criteria,
    make_disabled_tool_shims,
)
from evals.harness.metrics import EvalResult, ToolEvalResult


# ---------------------------------------------------------------------------
# build_discovery_context
# ---------------------------------------------------------------------------

def test_context_tier0_mentions_clive_tools_and_categories():
    ctx = build_discovery_context(registry_tier=0)
    assert "clive-tools" in ctx
    assert "clive-tools list" in ctx
    assert "clive-tools info" in ctx
    # Tier 0 is the category index
    assert "data" in ctx


def test_context_tier0_does_not_leak_tool_names():
    """Tier 0 must not pre-name concrete tools — discovery is the eval."""
    ctx = build_discovery_context(registry_tier=0)
    assert "jq" not in ctx.split("clive-tools")[0]


def test_context_rejects_unknown_tier():
    with pytest.raises(ValueError):
        build_discovery_context(registry_tier=3)


# ---------------------------------------------------------------------------
# make_disabled_tool_shims
# ---------------------------------------------------------------------------

def test_shims_created_and_exit_127(tmp_path):
    shim_dir = make_disabled_tool_shims(str(tmp_path / "shims"), ["yq", "xsv"])
    for name in ("yq", "xsv"):
        path = os.path.join(shim_dir, name)
        assert os.path.isfile(path)
        assert os.stat(path).st_mode & stat.S_IXUSR
        proc = subprocess.run([path], capture_output=True)
        assert proc.returncode == 127
        assert name in proc.stderr.decode()


def test_shims_reject_bad_names(tmp_path):
    for bad in ("../evil", "a b", "", "/bin/sh", "foo;rm"):
        with pytest.raises(ValueError):
            make_disabled_tool_shims(str(tmp_path / "shims"), [bad])


def test_shims_empty_list_returns_none(tmp_path):
    assert make_disabled_tool_shims(str(tmp_path / "shims"), []) is None


# ---------------------------------------------------------------------------
# check_discovery_criteria
# ---------------------------------------------------------------------------

SCROLLBACK_JQ = f"""
{PROMPT_MARKER} clive-tools list
core (12 tools) | data (9 tools) | web (7 tools)
{PROMPT_MARKER} clive-tools list data
jq  yq  xsv  q  dasel  visidata
{PROMPT_MARKER} clive-tools info jq
jq — command-line JSON processor. PATTERNS: jq '.[] | .email' file.json
{PROMPT_MARKER} jq -r '.[].email' users.json > /tmp/clive/result.txt
{PROMPT_MARKER} cat /tmp/clive/result.txt
alice@example.com
charlie@example.com
"""


def test_full_criteria_pass():
    ok, fields, detail = check_discovery_criteria(
        {"must_use_commands": ["list", "info"], "expected_tool": "jq"},
        SCROLLBACK_JQ,
    )
    assert ok, detail
    assert fields["tool_used"] == "jq"
    assert fields["tool_expected"] == "jq"
    assert fields["tool_correct"] is True
    assert fields["discovery_turns"] == 3  # three clive-tools invocations
    assert fields["fallback_used"] is False


def test_issue_style_command_aliases():
    """gh#40 spells the commands 'tools' and 'tool_info'; accept both."""
    ok, _, detail = check_discovery_criteria(
        {"must_use_commands": ["tools", "tool_info"], "expected_tool": "jq"},
        SCROLLBACK_JQ,
    )
    assert ok, detail


def test_tool_name_in_output_text_does_not_count_as_usage():
    """'jq' appearing in clive-tools listing output is not tool usage."""
    scrollback = f"""
{PROMPT_MARKER} clive-tools list data
jq  yq  xsv
{PROMPT_MARKER} python3 -c 'print(1)'
"""
    ok, fields, _ = check_discovery_criteria(
        {"expected_tool": "jq"}, scrollback
    )
    assert not ok
    assert fields["tool_used"] != "jq"
    assert fields["tool_correct"] is False


def test_missing_discovery_commands_fails_with_detail():
    scrollback = f"{PROMPT_MARKER} jq '.x' f.json > /tmp/clive/result.txt\n"
    ok, _, detail = check_discovery_criteria(
        {"must_use_commands": ["list"], "expected_tool": "jq"}, scrollback
    )
    assert not ok
    assert "list" in detail


def test_expected_tools_alternation_all_must_match():
    scrollback = f"""
{PROMPT_MARKER} clive-tools list
{PROMPT_MARKER} curl -s http://x/api > raw.json
{PROMPT_MARKER} jq '.users' raw.json > /tmp/clive/result.csv
"""
    ok, fields, _ = check_discovery_criteria(
        {"expected_tools": ["curl|http", "xsv|jq|q"]}, scrollback
    )
    assert ok
    assert fields["pipeline_stages"] == 2

    ok2, _, detail2 = check_discovery_criteria(
        {"expected_tools": ["curl|http", "xsv|q"]}, scrollback
    )
    assert not ok2
    assert "xsv|q" in detail2


def test_expected_fallback_sets_fallback_used():
    scrollback = f"""
{PROMPT_MARKER} yq '.database.port' config.yaml
yq: command not found
{PROMPT_MARKER} python3 -c 'import yaml' 2>/dev/null || grep -A2 database config.yaml
{PROMPT_MARKER} grep -A2 'database:' config.yaml > /tmp/clive/result.txt
"""
    ok, fields, detail = check_discovery_criteria(
        {"expected_fallback": "dasel|grep|python3"}, scrollback
    )
    assert ok, detail
    assert fields["fallback_used"] is True


def test_empty_criteria_passes_trivially():
    ok, fields, _ = check_discovery_criteria({}, "anything")
    assert ok
    assert fields["discovery_turns"] == 0


def test_expected_flags_satisfied_sets_flags_correct():
    """A required flag present on the tool's invocation -> flags_correct True."""
    scrollback = f"{PROMPT_MARKER} jq -r '.[].email' users.json\n"
    ok, fields, detail = check_discovery_criteria(
        {"expected_flags": {"jq": ["-r"]}}, scrollback
    )
    assert ok, detail
    assert fields["flags_correct"] is True


def test_expected_flags_missing_flag_fails_with_detail():
    """A required flag absent from the tool's invocation -> flags_correct False,
    ok False, and the detail names the missing flag."""
    scrollback = f"{PROMPT_MARKER} jq '.[].email' users.json\n"
    ok, fields, detail = check_discovery_criteria(
        {"expected_flags": {"jq": ["-r"]}}, scrollback
    )
    assert not ok
    assert fields["flags_correct"] is False
    assert "-r" in detail


def test_no_expected_flags_keeps_flags_correct_true():
    """Back-compat: criteria without expected_flags leaves flags_correct True."""
    ok, fields, _ = check_discovery_criteria(
        {"expected_tool": "jq"}, SCROLLBACK_JQ
    )
    assert ok
    assert fields["flags_correct"] is True


# ---------------------------------------------------------------------------
# ToolEvalResult
# ---------------------------------------------------------------------------

def test_tool_eval_result_extends_eval_result():
    r = ToolEvalResult(
        task_id="t", layer=5, tool="discovery", passed=True,
        turns_used=4, min_turns=3, prompt_tokens=10, completion_tokens=5,
        elapsed_seconds=1.0, detail="ok",
        tool_used="jq", tool_expected="jq", discovery_turns=2,
    )
    assert isinstance(r, EvalResult)
    assert r.tool_correct is True
    assert r.fallback_used is False
    assert r.pipeline_stages == 0
    assert r.flags_correct is True
    assert r.turn_efficiency == 0.75


# ---------------------------------------------------------------------------
# layer5 tasks.json schema
# ---------------------------------------------------------------------------

def _load_layer5_tasks():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "evals", "layer5", "discovery", "tasks.json")
    with open(path) as f:
        return json.load(f)


def test_layer5_tasks_schema():
    tasks = _load_layer5_tasks()
    assert len(tasks) >= 5
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate eval ids"
    for t in tasks:
        assert t["layer"] == 5
        assert t["tool"] == "discovery"
        assert t["mode"] == "interactive"
        assert t["task"]
        assert t["success_criteria"]["type"] == "deterministic"
        assert t["success_criteria"]["check"]
        assert "discovery_criteria" in t
        assert t["initial_state"]["registry_tier"] == 0
        assert t["max_turns"] >= t["min_turns"] >= 1


def test_layer5_fixtures_exist():
    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.join(here, "..", "evals", "layer5", "discovery")
    for t in _load_layer5_tasks():
        fs = t.get("initial_state", {}).get("filesystem")
        if fs:
            assert os.path.isdir(os.path.join(base, fs)), f"missing fixture {fs}"


def test_layer5_disabled_tools_are_valid_names():
    for t in _load_layer5_tasks():
        for name in t.get("initial_state", {}).get("disabled_tools", []):
            # Same validation the shim builder applies
            make_disabled_tool_shims  # imported above; names checked at runtime
            assert name.replace("-", "").replace("_", "").isalnum()


# ---------------------------------------------------------------------------
# script-mode evidence (L3 pipelines: tools live in the generated script,
# not on pane prompt lines)
# ---------------------------------------------------------------------------

def test_script_text_counts_as_commands():
    script = (
        "#!/bin/sh\n"
        "# count lines per py file\n"
        "find . -name '*.py' | xargs wc -l | sort -rn | head -3 > /tmp/clive/result.txt\n"
    )
    ok, fields, detail = check_discovery_criteria(
        {"expected_tools": ["find|fd", "wc", "sort"]}, "", script_text=script
    )
    assert ok, detail
    assert fields["pipeline_stages"] == 3


def test_script_comment_lines_are_ignored():
    script = "#!/bin/sh\n# jq would be nice here\necho no tools\n"
    ok, _, _ = check_discovery_criteria(
        {"expected_tool": "jq"}, "", script_text=script
    )
    assert not ok


def test_fallback_expected_field_set_from_criteria():
    _, with_fb, _ = check_discovery_criteria({"expected_fallback": "grep"}, "")
    _, without_fb, _ = check_discovery_criteria({"expected_tool": "jq"}, "")
    assert with_fb["fallback_expected"] is True
    assert without_fb["fallback_expected"] is False


# ---------------------------------------------------------------------------
# EvalReport tool metrics
# ---------------------------------------------------------------------------

def _tr(**kw):
    base = dict(
        task_id="t", layer=5, tool="discovery", passed=True,
        turns_used=4, min_turns=3, prompt_tokens=1, completion_tokens=1,
        elapsed_seconds=0.1, detail="d",
    )
    base.update(kw)
    return ToolEvalResult(**base)


def test_report_tool_metrics():
    from evals.harness.metrics import EvalReport
    results = [
        _tr(task_id="a", tool_expected="jq", tool_used="jq",
            tool_correct=True, discovery_turns=2, flags_correct=True),
        _tr(task_id="b", tool_expected="rg", tool_used="grep",
            tool_correct=False, passed=False, discovery_turns=4,
            flags_correct=False),
        _tr(task_id="c", layer=3, tool="pipeline", pipeline_stages=3),
        _tr(task_id="d", fallback_expected=True, fallback_used=True),
        EvalResult(task_id="plain", layer=2, tool="shell", passed=True,
                   turns_used=1, min_turns=1, prompt_tokens=1,
                   completion_tokens=1, elapsed_seconds=0.1, detail="d"),
    ]
    report = EvalReport(results)
    assert report.tool_accuracy == pytest.approx(3 / 4)
    # c and d default flags_correct=True; only b is False -> 3 of 4 tool results.
    assert report.flag_accuracy == pytest.approx(3 / 4)
    assert report.discovery_efficiency == pytest.approx((2 + 4) / 2)
    assert report.pipeline_success_rate == pytest.approx(1.0)
    assert report.fallback_success_rate == pytest.approx(1.0)
    d = report.to_dict()
    assert d["tool_metrics"]["tool_accuracy"] == round(3 / 4, 3)


def test_report_tool_metrics_absent_without_tool_results():
    from evals.harness.metrics import EvalReport
    plain = EvalResult(task_id="p", layer=2, tool="shell", passed=True,
                       turns_used=1, min_turns=1, prompt_tokens=1,
                       completion_tokens=1, elapsed_seconds=0.1, detail="d")
    report = EvalReport([plain])
    assert "tool_metrics" not in report.to_dict()


# ---------------------------------------------------------------------------
# layer3 pipeline + layer2 per-tool tasks schema
# ---------------------------------------------------------------------------

def _load_tasks(rel):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "evals", rel)) as f:
        return json.load(f)


def test_layer3_pipeline_tasks_schema():
    tasks = _load_tasks("layer3/pipelines/tasks.json")
    assert len(tasks) >= 3
    for t in tasks:
        assert t["layer"] == 3
        assert t["mode"] == "script"
        assert t["discovery_criteria"]["expected_tools"]
        assert t["success_criteria"]["type"] == "deterministic"
        # pipeline evals must NOT inject discovery context
        assert "registry_tier" not in t.get("initial_state", {})


def test_layer2_per_tool_tasks_schema():
    for rel, expected in [
        ("layer2/jq/tasks.json", "jq"),
        ("layer2/rg/tasks.json", "rg"),
    ]:
        tasks = _load_tasks(rel)
        assert len(tasks) >= 2
        for t in tasks:
            assert t["layer"] == 2
            assert t["mode"] == "script"
            assert expected in t["discovery_criteria"]["expected_tool"]
