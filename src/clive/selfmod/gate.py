"""Deterministic gate — the immutable safety anchor.

This file is IMMUTABLE. No LLM role can modify it. It performs regex-based
scanning on proposed changes and has unconditional veto power.

The gate cannot be "talked past" — it runs deterministic pattern matching,
not LLM inference.
"""

import re
from pathlib import Path

from selfmod.constitution import get_tier, highest_tier, required_approvals

# Banned patterns: (regex, description)
BANNED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"os\.system\s*\("), "os.system() call"),
    (re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"), "subprocess with shell=True"),
    (re.compile(r"(?<!\w)eval\s*\("), "eval() call"),
    (re.compile(r"(?<!\w)exec\s*\("), "exec() call"),
    (re.compile(r"import\s+ctypes"), "ctypes import"),
    (re.compile(r"importlib\.reload\s*\("), "importlib.reload()"),
    (re.compile(r"['\"][A-Za-z0-9+/=]{100,}['\"]"), "obfuscated base64 string"),
    (re.compile(r"(?:urllib|requests|httpx|socket)\b"), "network library in selfmod"),
    (re.compile(r"__import__\s*\("), "dynamic __import__()"),
]

# Paths that can never be touched
ABSOLUTE_IMMUTABLE = {
    "selfmod/gate.py",
    ".clive/constitution.md",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def check_proposal(
    files: dict[str, str],
    approvals: dict[str, str],
) -> dict:
    """Check a modification proposal against deterministic rules.

    Args:
        files: mapping of relative filepath → proposed new content
        approvals: mapping of role name → approval status ("approved"/"rejected"/"advisory")

    Returns:
        dict with keys:
            allowed: bool
            reason: str (human-readable)
            violations: list of specific violations found
    """
    violations = []

    # 1. Check immutable files
    for filepath in files:
        rel = _normalize_path(filepath)
        if rel in ABSOLUTE_IMMUTABLE:
            violations.append(f"IMMUTABLE: cannot modify {rel}")
        if rel.startswith(".clive/audit/"):
            violations.append(f"IMMUTABLE: cannot modify audit trail ({rel})")

    if violations:
        return {"allowed": False, "reason": "Immutable files targeted", "violations": violations}

    # 2. Check tier-based approval requirements
    paths = list(files.keys())
    tier = highest_tier(paths)
    required = required_approvals(tier)

    if required is None:
        violations.append(f"Tier {tier}: modifications not allowed")
        return {"allowed": False, "reason": f"Tier {tier} is immutable", "violations": violations}

    missing = []
    for role in required:
        status = approvals.get(role, "missing")
        if status != "approved":
            missing.append(f"{role}: {status}")

    if missing:
        violations.append(f"Tier {tier} requires: {', '.join(required)}. Missing/rejected: {', '.join(missing)}")

    # 3. Scan content for banned patterns
    for filepath, content in files.items():
        rel = _normalize_path(filepath)

        # Banned patterns apply to selfmod modules more strictly
        is_selfmod = rel.startswith("selfmod/")

        for pattern, description in BANNED_PATTERNS:
            # Network library check only applies to selfmod modules
            if "network library" in description and not is_selfmod:
                continue

            matches = pattern.findall(content)
            if matches:
                violations.append(f"{rel}: banned pattern: {description}")

    # 4. Check for file tier escalation attempts
    for filepath, content in files.items():
        rel = _normalize_path(filepath)
        # If the file modifies constitution.py's FILE_TIERS, reject
        if rel == "selfmod/constitution.py" and "FILE_TIERS" in content:
            # Check if any tier was downgraded
            violations.append(f"{rel}: potential tier escalation (modifies FILE_TIERS)")

    if violations:
        return {
            "allowed": False,
            "reason": f"Gate rejected: {len(violations)} violation(s)",
            "violations": violations,
        }

    return {"allowed": True, "reason": "All checks passed", "violations": []}


def _normalize_path(filepath: str) -> str:
    """Normalize a file path to be relative to project root."""
    try:
        return str(Path(filepath).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return filepath
