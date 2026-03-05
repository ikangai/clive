"""Self-modification pipeline — orchestrates the full flow.

Flow: Propose → Review → Audit → Gate → Snapshot → Apply
Each step can abort the pipeline. The gate has final veto.
"""

import logging
import uuid
from pathlib import Path

from selfmod import is_enabled
from selfmod.audit import log_attempt, get_session_count
from selfmod.constitution import (
    get_tier, highest_tier, required_approvals, PROJECT_ROOT,
)
from selfmod.gate import check_proposal
from selfmod.proposer import propose
from selfmod.reviewer import review
from selfmod.auditor import audit
from selfmod.workspace import snapshot, apply_changes

log = logging.getLogger(__name__)

MAX_MODIFICATIONS_PER_SESSION = 5


class SelfModError(Exception):
    pass


class SelfModResult:
    """Result of a self-modification attempt."""

    def __init__(self):
        self.proposal_id: str = ""
        self.success: bool = False
        self.stage: str = ""
        self.message: str = ""
        self.proposal: dict | None = None
        self.review: dict | None = None
        self.audit_result: dict | None = None
        self.gate_result: dict | None = None
        self.snapshot_tag: str = ""
        self.tokens: dict = {"prompt": 0, "completion": 0}

    def _add_tokens(self, pt: int, ct: int) -> None:
        self.tokens["prompt"] += pt
        self.tokens["completion"] += ct


def run_pipeline(
    goal: str,
    on_status: callable = None,
) -> SelfModResult:
    """Run the full self-modification pipeline.

    Args:
        goal: what the user wants to change about clive
        on_status: optional callback(stage, message) for progress updates

    Returns:
        SelfModResult with full details
    """
    result = SelfModResult()
    result.proposal_id = uuid.uuid4().hex[:12]

    def status(stage: str, msg: str) -> None:
        result.stage = stage
        result.message = msg
        if on_status:
            on_status(stage, msg)
        log.info(f"selfmod [{stage}]: {msg}")

    # Pre-flight checks
    if not is_enabled():
        result.message = "Self-modification is disabled. Set CLIVE_EXPERIMENTAL_SELFMOD=1 in .env"
        result.stage = "disabled"
        return result

    count = get_session_count()
    if count >= MAX_MODIFICATIONS_PER_SESSION:
        result.message = f"Rate limit: {count}/{MAX_MODIFICATIONS_PER_SESSION} modifications this session"
        result.stage = "rate_limited"
        return result

    # Step 1: Identify relevant files
    status("analyzing", "Identifying relevant files...")
    file_context = _gather_context(goal)

    # Step 2: Propose
    status("proposing", "Generating modification proposal...")
    try:
        proposal, pt, ct = propose(goal, file_context)
        result._add_tokens(pt, ct)
        result.proposal = proposal
    except Exception as e:
        result.message = f"Proposal failed: {e}"
        result.stage = "proposal_failed"
        _log_failure(result)
        return result

    files = proposal.get("files", {})
    if not files:
        result.message = "Proposal contains no file changes"
        result.stage = "proposal_empty"
        _log_failure(result)
        return result

    tier = highest_tier(list(files.keys()))
    status("proposing", f"Proposal: {len(files)} file(s), tier {tier}")

    # Step 3: Review (if tier requires it)
    required = required_approvals(tier)
    approvals = {"proposer": "approved"}  # proposer always approves their own

    if required is None:
        result.message = f"Tier {tier} is immutable — cannot modify"
        result.stage = "tier_immutable"
        _log_failure(result)
        return result

    if "reviewer" in required:
        status("reviewing", "Code review in progress...")
        try:
            current_files = _read_current_files(list(files.keys()))
            review_result, pt, ct = review(proposal, current_files)
            result._add_tokens(pt, ct)
            result.review = review_result

            verdict = review_result.get("verdict", "rejected")
            approvals["reviewer"] = verdict
            status("reviewing", f"Reviewer: {verdict}")

            if verdict == "rejected":
                issues = review_result.get("issues", [])
                result.message = f"Reviewer rejected: {'; '.join(issues[:3])}"
                result.stage = "reviewer_rejected"
                _log_failure(result, approvals=approvals)
                return result
        except Exception as e:
            result.message = f"Review failed: {e}"
            result.stage = "review_failed"
            _log_failure(result, approvals=approvals)
            return result
    else:
        review_result = {"verdict": "not_required", "issues": []}
        result.review = review_result

    # Step 4: Audit (if tier requires it)
    if "auditor" in required:
        status("auditing", "Governance audit in progress...")
        try:
            audit_result, pt, ct = audit(proposal, review_result)
            result._add_tokens(pt, ct)
            result.audit_result = audit_result

            verdict = audit_result.get("verdict", "rejected")
            approvals["auditor"] = verdict
            status("auditing", f"Auditor: {verdict}")

            if verdict == "rejected":
                issues = audit_result.get("governance_issues", [])
                result.message = f"Auditor rejected: {'; '.join(issues[:3])}"
                result.stage = "auditor_rejected"
                _log_failure(result, approvals=approvals)
                return result
        except Exception as e:
            result.message = f"Audit failed: {e}"
            result.stage = "audit_failed"
            _log_failure(result, approvals=approvals)
            return result
    else:
        audit_result = {"verdict": "not_required"}
        result.audit_result = audit_result

    # Step 5: Deterministic gate (final veto)
    status("gate", "Deterministic gate check...")
    gate_result = check_proposal(files, approvals)
    result.gate_result = gate_result

    if not gate_result["allowed"]:
        violations = gate_result.get("violations", [])
        result.message = f"Gate rejected: {'; '.join(violations[:3])}"
        result.stage = "gate_rejected"
        _log_failure(result, approvals=approvals, gate_result=gate_result)
        return result

    status("gate", "Gate: passed")

    # Step 6: Snapshot and apply
    status("applying", "Creating snapshot...")
    try:
        tag = snapshot(label=result.proposal_id)
        result.snapshot_tag = tag
    except Exception as e:
        result.message = f"Snapshot failed: {e}"
        result.stage = "snapshot_failed"
        _log_failure(result, approvals=approvals, gate_result=gate_result)
        return result

    status("applying", "Applying changes...")
    try:
        apply_changes(files)
    except Exception as e:
        result.message = f"Apply failed: {e}"
        result.stage = "apply_failed"
        _log_failure(result, approvals=approvals, gate_result=gate_result)
        return result

    # Success
    result.success = True
    result.stage = "complete"
    result.message = proposal.get("description", "Modification applied")

    log_attempt(
        proposal_id=result.proposal_id,
        action="apply",
        files=list(files.keys()),
        tier=tier,
        roles=approvals,
        gate_result=gate_result,
        outcome="applied",
        details=result.message,
    )

    status("complete", result.message)
    return result


def _gather_context(goal: str) -> dict[str, str]:
    """Read relevant project files based on the goal keywords.

    Instead of sending the entire codebase (which blows up token counts and
    causes truncated responses), we send only the files most likely relevant
    to the goal plus a brief index of all files.
    """
    context = {}
    goal_lower = goal.lower()

    # Map keywords to likely relevant files
    keyword_files = {
        "tui": ["tui.py"],
        "input": ["tui.py"],
        "slash": ["tui.py"],
        "command": ["tui.py", "clive.py"],
        "ui": ["tui.py"],
        "prompt": ["prompts.py", "tui.py"],
        "plan": ["planner.py", "prompts.py"],
        "execut": ["executor.py"],
        "llm": ["llm.py"],
        "model": ["llm.py", "models.py"],
        "provider": ["llm.py", "tui.py"],
        "tool": ["toolsets.py"],
        "profile": ["toolsets.py", "tui.py"],
        "session": ["session.py"],
        "tmux": ["session.py"],
        "pane": ["session.py", "models.py"],
        "complet": ["completion.py"],
        "install": ["install.sh"],
        "selfmod": ["selfmod/pipeline.py"],
    }

    # Collect relevant files
    relevant = set()
    for keyword, files in keyword_files.items():
        if keyword in goal_lower:
            relevant.update(files)

    # Always include tui.py for UI changes (most common selfmod target)
    if not relevant:
        relevant.add("tui.py")

    # Read relevant files in full
    for name in relevant:
        path = PROJECT_ROOT / name
        if path.exists():
            context[name] = path.read_text()

    # Add a brief index of ALL project files (so proposer knows what exists)
    all_files = []
    for name in sorted(PROJECT_ROOT.glob("*.py")):
        all_files.append(name.name)
    for name in sorted((PROJECT_ROOT / "selfmod").glob("*.py")):
        all_files.append(f"selfmod/{name.name}")
    context["__FILE_INDEX__"] = (
        "All project files (read the ones you need with their full content above):\n"
        + "\n".join(f"  {f}" for f in all_files)
    )

    return context


def _read_current_files(filepaths: list[str]) -> dict[str, str]:
    """Read current content of files that will be modified."""
    result = {}
    for fp in filepaths:
        path = PROJECT_ROOT / fp
        if path.exists():
            result[fp] = path.read_text()
        else:
            result[fp] = ""  # new file
    return result


def _log_failure(
    result: SelfModResult,
    approvals: dict[str, str] | None = None,
    gate_result: dict | None = None,
) -> None:
    """Log a failed modification attempt."""
    files = []
    if result.proposal and result.proposal.get("files"):
        files = list(result.proposal["files"].keys())

    tier = highest_tier(files) if files else "UNKNOWN"

    log_attempt(
        proposal_id=result.proposal_id,
        action="reject",
        files=files,
        tier=tier,
        roles=approvals or {},
        gate_result=gate_result or {},
        outcome=result.stage,
        details=result.message,
    )
