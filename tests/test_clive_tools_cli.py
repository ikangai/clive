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
