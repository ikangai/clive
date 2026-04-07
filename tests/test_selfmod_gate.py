"""Tests for selfmod gate — deterministic safety anchor.

These test the gate's pattern matching directly. Zero false acceptances
is the critical property.
"""
from selfmod.gate import check_proposal


# ─── Banned Pattern Detection ────────────────────────────────────────────────

def test_rejects_eval():
    result = check_proposal(
        files={"tools/helper.py": "data = eval(input())"},
        approvals={"proposer": "approved"},
    )
    assert not result["allowed"]
    assert any("eval()" in v for v in result["violations"])


def test_rejects_os_system():
    result = check_proposal(
        files={"tools/helper.py": "import os\nos.system('rm -rf /')"},
        approvals={"proposer": "approved"},
    )
    assert not result["allowed"]
    assert any("os.system()" in v for v in result["violations"])


def test_rejects_shell_true():
    result = check_proposal(
        files={"tools/helper.py": "subprocess.run('ls', shell=True)"},
        approvals={"proposer": "approved"},
    )
    assert not result["allowed"]
    assert any("shell=True" in v for v in result["violations"])


def test_rejects_exec():
    result = check_proposal(
        files={"tools/helper.py": "exec(open('malicious.py').read())"},
        approvals={"proposer": "approved"},
    )
    assert not result["allowed"]
    assert any("exec()" in v for v in result["violations"])


def test_rejects_dynamic_import():
    result = check_proposal(
        files={"tools/helper.py": "mod = __import__('os')"},
        approvals={"proposer": "approved"},
    )
    assert not result["allowed"]
    assert any("__import__" in v for v in result["violations"])


def test_rejects_network_in_selfmod():
    result = check_proposal(
        files={"selfmod/proposer.py": "import requests\nrequests.get('http://evil.com')"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]
    assert any("network library" in v for v in result["violations"])


def test_allows_network_outside_selfmod():
    """Network libraries are fine in non-selfmod code."""
    result = check_proposal(
        files={"tools/fetcher.py": "import requests\nrequests.get('http://api.example.com')"},
        approvals={"proposer": "approved"},
    )
    assert result["allowed"]


# ─── Immutable File Protection ───────────────────────────────────────────────

def test_rejects_gate_modification():
    result = check_proposal(
        files={"selfmod/gate.py": "# hacked"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]
    assert any("IMMUTABLE" in v for v in result["violations"])


def test_rejects_constitution_modification():
    result = check_proposal(
        files={".clive/constitution.md": "# new rules"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]
    assert any("IMMUTABLE" in v for v in result["violations"])


def test_rejects_audit_trail_modification():
    result = check_proposal(
        files={".clive/audit/12345.json": "{}"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]
    assert any("audit trail" in v for v in result["violations"])


# ─── Tier-Based Approval ─────────────────────────────────────────────────────

def test_governance_requires_all_three():
    """GOVERNANCE tier needs proposer + reviewer + auditor."""
    result = check_proposal(
        files={"selfmod/proposer.py": "# safe change\npass"},
        approvals={"proposer": "approved"},
    )
    assert not result["allowed"]


def test_governance_passes_with_all_three():
    result = check_proposal(
        files={"selfmod/audit.py": "# safe change\npass"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert result["allowed"]


def test_core_requires_reviewer():
    """CORE tier needs proposer + reviewer."""
    result = check_proposal(
        files={"planner.py": "# safe change\npass"},
        approvals={"proposer": "approved"},
    )
    assert not result["allowed"]


def test_core_passes_with_reviewer():
    result = check_proposal(
        files={"planner.py": "# safe change\npass"},
        approvals={"proposer": "approved", "reviewer": "approved"},
    )
    assert result["allowed"]


def test_open_tier_proposer_only():
    """OPEN tier only needs proposer."""
    result = check_proposal(
        files={"tools/new_tool.sh": "#!/bin/bash\necho hello"},
        approvals={"proposer": "approved"},
    )
    assert result["allowed"]


# ─── Tier Escalation Detection ───────────────────────────────────────────────

def test_rejects_tier_escalation():
    result = check_proposal(
        files={"selfmod/constitution.py": "FILE_TIERS = {'gate.py': 'OPEN'}"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]
    assert any("tier escalation" in v for v in result["violations"])


# ─── Clean Proposals Pass ────────────────────────────────────────────────────

def test_clean_open_proposal():
    result = check_proposal(
        files={"tools/youtube.sh": "#!/bin/bash\necho 'fetching video...'"},
        approvals={"proposer": "approved"},
    )
    assert result["allowed"]
    assert result["violations"] == []
