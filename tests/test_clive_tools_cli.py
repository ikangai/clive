"""clive-tools CLI: in-pane discovery for agents."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "clive-tools"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
    )


def test_list_no_args_shows_categories():
    r = _run("list")
    assert r.returncode == 0
    assert "data" in r.stdout and "web" in r.stdout


def test_list_with_category_shows_tools():
    r = _run("list", "data")
    assert r.returncode == 0
    assert "jq" in r.stdout


def test_info_shows_card():
    r = _run("info", "jq")
    assert r.returncode == 0
    assert r.stdout.startswith("[jq]")


def test_info_unknown_is_nonzero():
    r = _run("info", "not_a_real_tool")
    assert r.returncode != 0


def test_exit_codes_distinguish_not_found_from_usage():
    """rc 1 = tool not found; rc 2 = usage error. Don't collapse them."""
    assert _run("info", "not_a_real_tool").returncode == 1
    assert _run("info").returncode == 2
    assert _run("list", "bogus_category").returncode == 2
    assert _run("nonsense").returncode == 2


def test_rejects_extra_positional_args():
    """An LLM that miswrites the command should not get rc=0."""
    assert _run("list", "data", "extra").returncode == 2
    assert _run("info", "jq", "extra").returncode == 2


def test_planner_prompt_advertises_cli():
    """The discovery hint must reach the planner prompt."""
    from llm.prompts import build_planner_prompt
    prompt = build_planner_prompt(tools_summary="(test stub)")
    assert "clive-tools" in prompt
