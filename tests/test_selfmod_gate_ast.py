"""Tests for the AST-based selfmod gate scanner (Phase 2, Bug H6).

The regex-based BANNED_PATTERNS has documented bypasses (see
debug/260520-2042-autonomous-codebase-sweep/findings.md H6):
  - subprocess.run(build_cmd(host), shell=True): `[^)]*` halts at inner `)`
  - from ctypes import X: regex only matches `import ctypes`
  - from ctypes.util import find_library: same
  - import urllib3: `\b` after `urllib` fails before `3` (still a word char)
  - import websockets: `\b` after `socket` fails before `s`

The gate is documented as the "deterministic safety anchor that cannot be
talked past" — these bypasses violate that claim. Right fix is ast-based
scanning for .py content (regex still applies to non-.py files for the
obfuscated-base64 / network-string checks).

These tests pin the desired post-fix behavior: every bypass shape above
is rejected, and the existing pattern detection continues to work.
"""
import pytest

from selfmod.gate import check_proposal


# --- Each bypass shape from H6 ---

def test_blocks_subprocess_run_with_inner_call_shell_true():
    """The original regex `subprocess\\.\\w+\\([^)]*shell\\s*=\\s*True`
    stops at the first `)` (inside `build_cmd()`), so `shell=True` lives
    in a different parenthesis pair the regex never sees.
    """
    result = check_proposal(
        files={"selfmod/proposer.py": (
            "import subprocess\n"
            "def go(host):\n"
            "    return subprocess.run(build_cmd(host), shell=True)\n"
        )},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"], (
        "subprocess.run(fn(), shell=True) reached an approved state — "
        "Bug H6 #1 reproduced. AST scanner must walk keyword=True."
    )


def test_blocks_subprocess_popen_with_inner_call_shell_true():
    """Same shape for Popen — `subprocess\\.\\w+` matches but `[^)]*`
    stops at inner `)`. AST scan must recognize the subprocess attribute
    call regardless of arg expression complexity.
    """
    result = check_proposal(
        files={"selfmod/proposer.py": (
            "import subprocess\n"
            "subprocess.Popen(list(args), shell=True, cwd=base)\n"
        )},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_blocks_from_ctypes_import():
    """`from ctypes import X` was not matched by `import\\s+ctypes`."""
    result = check_proposal(
        files={"selfmod/proposer.py": "from ctypes import POINTER, c_int\n"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_blocks_from_ctypes_util_import():
    """`from ctypes.util import find_library` was also missed."""
    result = check_proposal(
        files={"selfmod/proposer.py": "from ctypes.util import find_library\n"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_blocks_import_urllib3():
    """`urllib3` was not matched by `\\burllib\\b` — `3` is a word char,
    so no word boundary after `urllib`.
    """
    result = check_proposal(
        files={"selfmod/proposer.py": "import urllib3\n"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_blocks_import_websockets():
    """`websockets` was not matched by `\\bsocket\\b` — `s` is a word char,
    so no word boundary after `socket`.
    """
    result = check_proposal(
        files={"selfmod/proposer.py": "import websockets\n"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_blocks_from_urllib3_import():
    """Symmetric to ctypes — `from urllib3 import PoolManager` also a path."""
    result = check_proposal(
        files={"selfmod/proposer.py": "from urllib3 import PoolManager\n"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


# --- Additional patterns the AST scanner should catch ---

def test_blocks_os_popen():
    """os.popen runs a shell command — same threat class as os.system,
    which the existing regex catches. Add to AST scanner for parity.
    """
    result = check_proposal(
        files={"selfmod/proposer.py": "import os\nos.popen('rm -rf /')\n"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


# --- Existing pattern coverage must still hold (regression guards) ---

@pytest.mark.parametrize("body,why", [
    ("import os\nos.system('x')", "os.system call"),
    ("data = eval(input())", "eval call"),
    ("exec(open('x.py').read())", "exec call"),
    ("import ctypes", "plain import ctypes"),
    ("import urllib", "plain urllib (no suffix)"),
    ("import requests", "requests"),
    ("import httpx", "httpx"),
    ("import socket", "plain socket"),
    ("mod = __import__('os')", "dynamic __import__"),
    ("import importlib\nimportlib.reload(x)", "importlib.reload"),
    ("subprocess.run('ls', shell=True)", "simple shell=True still caught"),
])
def test_regression_existing_python_patterns_still_blocked(body, why):
    result = check_proposal(
        files={"selfmod/proposer.py": body},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"], f"Regression: {why} no longer blocked"


# --- Non-Python content: regex must still apply (markdown, shell) ---

def test_obfuscated_base64_string_still_blocked_in_markdown():
    """The obfuscated-base64 pattern is content-shape, not language-shape.
    Don't lose it when refactoring around AST.
    """
    long_b64 = "A" * 120
    result = check_proposal(
        files={"docs/x.md": f"see this: '{long_b64}'"},
        approvals={"proposer": "approved"},
    )
    assert not result["allowed"]


def test_python_file_with_invalid_syntax_is_rejected():
    """A .py file the AST scanner cannot parse is rejected outright —
    not skipped (which would be a bypass) and not silently allowed.
    """
    result = check_proposal(
        files={"selfmod/proposer.py": "def broken(\n  this is not python\n"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


# --- Clean proposal still passes (regression) ---

def test_clean_python_proposal_still_passes():
    result = check_proposal(
        files={"tools/utility.py": "def add(a, b):\n    return a + b\n"},
        approvals={"proposer": "approved"},
    )
    assert result["allowed"]


def test_clean_python_with_subprocess_no_shell_passes():
    """subprocess without shell=True is permitted in non-selfmod code."""
    result = check_proposal(
        files={"tools/runner.py": (
            "import subprocess\n"
            "subprocess.run(['ls', '-la'], check=True)\n"
        )},
        approvals={"proposer": "approved"},
    )
    assert result["allowed"]
