"""Self-modification system for clive.

Experimental feature: enable with CLIVE_EXPERIMENTAL_SELFMOD=1 in .env.

Architecture: three independent LLM roles (Proposer, Reviewer, Auditor)
checked by a deterministic gate, governed by a constitution with file tiers.
"""

import os


def is_enabled() -> bool:
    return os.getenv("CLIVE_EXPERIMENTAL_SELFMOD", "").strip() == "1"
