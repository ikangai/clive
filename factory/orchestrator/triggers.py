"""Triggers — the gain governor (spec §9). The optimisation loop fires only when
reality has supplied new failure data. The loop can run no faster than ground
truth surfaces gaps."""
from __future__ import annotations

from ..common.store import Blackboard


def _last_proposal_at(store: Blackboard) -> str:
    cands = store.list_candidates()
    if not cands:
        return ""
    return max(c["created_at"] for c in cands)


def new_failures_since_last_proposal(store: Blackboard) -> int:
    """Count NEW ground-truth failures since the most recent proposal.

    Ground truth = the reigning champion (the deployed harness) failing on the
    working set — that is reality surfacing a gap. We deliberately EXCLUDE the
    optimiser's own candidate-evaluation losses: counting those would let a single
    weak candidate's eval failures re-arm the trigger and self-feed the loop,
    defeating the gain governor (§9: 'no faster than reality surfaces gaps')."""
    since = _last_proposal_at(store)
    champ = store.get_champion()
    if not champ:
        return 0
    rows = store.conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE outcome IN ('fail','error','blocked') "
        "AND partition = 'working' AND candidate_id = ? AND created_at > ?",
        (champ["id"], since)).fetchone()
    return int(rows["n"]) if rows else 0


def should_propose(store: Blackboard, cfg: dict) -> tuple[bool, int, int]:
    """Returns (fire, new_failures, threshold). The optimisation loop fires only
    when at least N new champion (ground-truth) failures have accumulated."""
    threshold = int(cfg.get("triggers", {}).get("new_failures_to_propose", 3))
    n = new_failures_since_last_proposal(store)
    return (n >= threshold, n, threshold)
