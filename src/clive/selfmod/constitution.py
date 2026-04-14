"""Load and verify the constitution governing self-modifications."""

import hashlib
from pathlib import Path

CONSTITUTION_PATH = Path(__file__).resolve().parent.parent.parent.parent / ".clive" / "constitution.md"

# File tier definitions — maps tier name to required approvals
TIERS = {
    "IMMUTABLE": {"approvals": None, "description": "Cannot be modified"},
    "GOVERNANCE": {
        "approvals": {"proposer", "reviewer", "auditor"},
        "description": "Requires unanimous approval",
    },
    "CORE": {
        "approvals": {"proposer", "reviewer"},
        "description": "Requires proposer + reviewer",
    },
    "STANDARD": {
        "approvals": {"proposer"},
        "description": "Proposer approval, reviewer advisory",
    },
    "OPEN": {
        "approvals": set(),
        "description": "Proposer can modify freely",
    },
}

# Path patterns → tier mapping (checked in order, first match wins)
FILE_TIERS: list[tuple[str, str]] = [
    # IMMUTABLE
    ("selfmod/gate.py", "IMMUTABLE"),
    (".clive/constitution.md", "IMMUTABLE"),
    (".clive/audit/", "IMMUTABLE"),
    # GOVERNANCE
    ("selfmod/", "GOVERNANCE"),
    (".env", "GOVERNANCE"),
    # CORE
    ("clive.py", "CORE"),
    ("llm.py", "CORE"),
    ("executor.py", "CORE"),
    ("planner.py", "CORE"),
    ("session.py", "CORE"),
    ("models.py", "CORE"),
    ("prompts.py", "CORE"),
    # STANDARD
    ("tui.py", "STANDARD"),
    ("toolsets.py", "STANDARD"),
    ("completion.py", "STANDARD"),
    ("install.sh", "STANDARD"),
    # OPEN is the default
]

# Project root for resolving relative paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_tier(filepath: str) -> str:
    """Determine the tier of a file path."""
    # Normalize to relative path from project root
    try:
        rel = str(Path(filepath).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        # Path not under project root — use as-is for pattern matching
        rel = filepath

    for pattern, tier in FILE_TIERS:
        if pattern.endswith("/"):
            if rel.startswith(pattern) or rel == pattern.rstrip("/"):
                return tier
        else:
            if rel == pattern:
                return tier
    return "OPEN"


def highest_tier(filepaths: list[str]) -> str:
    """Return the highest (most restrictive) tier among a set of files."""
    tier_order = ["OPEN", "STANDARD", "CORE", "GOVERNANCE", "IMMUTABLE"]
    max_idx = 0
    for fp in filepaths:
        tier = get_tier(fp)
        idx = tier_order.index(tier)
        max_idx = max(max_idx, idx)
    return tier_order[max_idx]


def required_approvals(tier: str) -> set[str] | None:
    """Return required approvals for a tier, or None if immutable."""
    return TIERS[tier]["approvals"]


def load_constitution() -> str:
    """Load the constitution text."""
    if not CONSTITUTION_PATH.exists():
        raise FileNotFoundError(f"Constitution not found: {CONSTITUTION_PATH}")
    return CONSTITUTION_PATH.read_text()


def constitution_hash() -> str:
    """SHA-256 hash of the constitution file."""
    text = load_constitution()
    return hashlib.sha256(text.encode()).hexdigest()
