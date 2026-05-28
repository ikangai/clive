"""Tests for the selfmod PROJECT_ROOT divergence fix (Audit C2, 2026-05-27).

selfmod/gate.py defines PROJECT_ROOT with four parents (= repo root) while
selfmod/workspace.py and selfmod/constitution.py use two parents (= src/clive/).
The audit reports that an absolute-path proposal normalizes to a key not in
FILE_TIERS, defaults to OPEN tier, and lets a proposer self-approve overwriting
gate.py itself. These tests pin the desired invariant: any proposal that
targets gate.py, the constitution, or the audit trail — by ANY shape of
filepath an attacker could craft — is rejected.

Tests also cover the defensive surface added by the fix: absolute paths and
`..` segments should be rejected at the gate boundary so silent path-shape
fallthroughs cannot reach FILE_TIERS in the first place.
"""
import os

import pytest

from selfmod.gate import check_proposal


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir)
)
GATE_ABS = os.path.join(REPO_ROOT, "src", "clive", "selfmod", "gate.py")
CONSTITUTION_ABS = os.path.join(REPO_ROOT, ".clive", "constitution.md")


def test_rejects_absolute_path_to_gate_py():
    """The audit's exploit shape: a Proposer supplies an absolute path to
    gate.py. The gate must reject regardless of path shape.
    """
    result = check_proposal(
        files={GATE_ABS: "# weakened gate"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"], (
        "Absolute-path proposal to gate.py reached an OPEN tier — "
        "audit C2 exploit reproduced. Fix the PROJECT_ROOT topology."
    )


def test_rejects_absolute_path_to_constitution():
    result = check_proposal(
        files={CONSTITUTION_ABS: "# rewritten"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_rejects_src_clive_prefixed_relative_path_to_gate():
    """Some callers may pass a path rooted at the repo (with src/clive/
    prefix) rather than the package-internal form. Both shapes must lose.
    """
    result = check_proposal(
        files={"src/clive/selfmod/gate.py": "# weakened gate"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_rejects_dotdot_traversal_to_gate():
    """A `..`-bearing relative path could in principle bypass tier lookups
    that rely on string equality. Reject up front rather than rely on
    .resolve()/CWD coincidence.
    """
    result = check_proposal(
        files={"../selfmod/gate.py": "# weakened"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_rejects_absolute_path_anywhere_in_proposal_set():
    """Even when only one file in a multi-file proposal uses an absolute
    path shape, the whole proposal is rejected — preserve the gate's
    all-or-nothing semantics.
    """
    result = check_proposal(
        files={
            "tools/helper.sh": "#!/bin/bash\necho hi",
            GATE_ABS: "# weakened gate",
        },
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]


def test_existing_relative_path_protection_still_works():
    """Regression guard: the pre-existing canonical path "selfmod/gate.py"
    continues to be rejected. The fix must not regress the working case.
    """
    result = check_proposal(
        files={"selfmod/gate.py": "# weakened gate"},
        approvals={"proposer": "approved", "reviewer": "approved", "auditor": "approved"},
    )
    assert not result["allowed"]
    assert any("IMMUTABLE" in v or "immutable" in v.lower() for v in result["violations"])


def test_open_tier_still_allows_clean_proposals():
    """Regression guard: a benign OPEN-tier proposal still passes."""
    result = check_proposal(
        files={"tools/helper.sh": "#!/bin/bash\necho hello"},
        approvals={"proposer": "approved"},
    )
    assert result["allowed"]
