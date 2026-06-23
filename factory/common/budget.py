"""Cost accounting + budget caps (spec §9, §11). Reuses clive's own
evals/harness/pricing.json when present, else a flat per-Mtok price."""
from __future__ import annotations

import json
import os
from typing import Optional

from . import config, paths


def _pricing_table() -> dict:
    path = os.path.join(paths.CLIVE_ROOT, "evals", "harness", "pricing.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def price(model: str, tokens: int) -> float:
    """Approximate USD cost for `tokens` total tokens under `model`."""
    table = _pricing_table()
    entry = table.get(model) or table.get(model.split("/")[-1])
    if isinstance(entry, dict):
        # pricing.json typically carries per-Mtok input/output; we only track a
        # single aggregate token count, so use the mean of in/out as an estimate.
        rate = (entry.get("input", 0) + entry.get("output", 0)) / 2.0 or \
               entry.get("price", 0)
        return rate * tokens / 1_000_000.0
    per_mtok = config.load_config().get("budget", {}).get("default_price_per_mtok", 1.0)
    return per_mtok * tokens / 1_000_000.0


class BudgetGuard:
    """Round-level hard ceiling. Circuit breaker for runaway cost."""

    def __init__(self, round_max_tokens: Optional[int] = None):
        cfg = config.load_config().get("budget", {})
        self.round_max_tokens = round_max_tokens or cfg.get("round_max_tokens", 400000)
        self.spent = 0

    def add(self, tokens: int) -> None:
        self.spent += max(0, int(tokens))

    def remaining(self) -> int:
        return max(0, self.round_max_tokens - self.spent)

    def exceeded(self) -> bool:
        return self.spent >= self.round_max_tokens
