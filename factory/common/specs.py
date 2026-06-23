"""clive harness spec format + validator (spec §4).

A spec is YAML partitioned by verifiability:
  meta:   {version, parent, hash}
  open:   {system_prompt, command_affordances, observation_policy,
           recovery_policy, skills}        # MUTABLE — grounded by the battery
  frozen: {permission_gates, scope_limits, destructive_action_policy}  # OUT of
                                            # the mutation space; human-only

Rules enforced here (hard):
- hash = sha256(canonical(open) + canonical(frozen)); a tampered frozen block is
  detectable (verify_hash).
- A candidate's frozen block must be canonically identical to the champion's —
  else REJECT (it touched frozen).
- open must differ from the parent and change at most `max_changed_open_keys`
  top-level keys ("one bounded change").
- meta.parent must be set.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import yaml

OPEN_KEYS = ["system_prompt", "command_affordances", "observation_policy",
             "recovery_policy", "skills"]
FROZEN_KEYS = ["permission_gates", "scope_limits", "destructive_action_policy"]


def canonical_json(obj: Any) -> str:
    """Deterministic serialization for hashing/diffing (sorted keys, no spaces)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(open_block: Any, frozen_block: Any) -> str:
    h = hashlib.sha256()
    h.update(canonical_json(open_block).encode("utf-8"))
    h.update(canonical_json(frozen_block).encode("utf-8"))
    return h.hexdigest()


def load_spec(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        spec = yaml.safe_load(fh) or {}
    spec.setdefault("meta", {})
    spec.setdefault("open", {})
    spec.setdefault("frozen", {})
    return spec


def dump_spec(spec: dict, path: str) -> None:
    # Stamp the hash so the on-disk file is self-verifying. Operate on a copy so
    # the caller's dict is not mutated as a side effect.
    out = {**spec, "meta": {**(spec.get("meta") or {}),
                            "hash": compute_hash(spec.get("open", {}), spec.get("frozen", {}))}}
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(out, fh, sort_keys=False, default_flow_style=False,
                       allow_unicode=True, width=100)


def verify_hash(spec: dict) -> bool:
    """True iff meta.hash matches the content. A False here means open or frozen
    was tampered with after the spec was sealed."""
    want = (spec.get("meta") or {}).get("hash")
    got = compute_hash(spec.get("open", {}), spec.get("frozen", {}))
    return bool(want) and want == got


def open_diff(parent_open: dict, cand_open: dict) -> dict:
    """Top-level keys of `open` that differ between parent and candidate."""
    changed: dict[str, dict] = {}
    keys = set(parent_open or {}) | set(cand_open or {})
    for k in sorted(keys):
        a = (parent_open or {}).get(k)
        b = (cand_open or {}).get(k)
        if canonical_json(a) != canonical_json(b):
            changed[k] = {"from": a, "to": b}
    return changed


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    diff: dict = field(default_factory=dict)
    changed_keys: list[str] = field(default_factory=list)


def validate_candidate(candidate: dict, champion: dict, *,
                       max_changed_open_keys: int = 1) -> ValidationResult:
    """Validate a candidate spec against the champion. Pure function."""
    errors: list[str] = []

    meta = candidate.get("meta") or {}
    if not meta.get("parent"):
        errors.append("meta.parent is required")

    # The candidate's own hash must be self-consistent (no tampering).
    if meta.get("hash") and not verify_hash(candidate):
        errors.append("meta.hash does not match content (open/frozen tampered)")

    # FROZEN is outside the mutation space: it must equal the champion's exactly.
    cand_frozen = candidate.get("frozen", {})
    champ_frozen = champion.get("frozen", {})
    if canonical_json(cand_frozen) != canonical_json(champ_frozen):
        errors.append("candidate mutates the frozen block (rejected: frozen is "
                      "outside the mutation space)")

    # OPEN must change, and change boundedly.
    parent_open = champion.get("open", {})
    cand_open = candidate.get("open", {})
    diff = open_diff(parent_open, cand_open)
    changed_keys = list(diff.keys())
    if not changed_keys:
        errors.append("candidate is identical to its parent's open block (no change)")
    if len(changed_keys) > max_changed_open_keys:
        errors.append(
            f"candidate changes {len(changed_keys)} open keys "
            f"({', '.join(changed_keys)}); max allowed is {max_changed_open_keys} "
            f"(one bounded change)")

    return ValidationResult(ok=not errors, errors=errors, diff=diff,
                            changed_keys=changed_keys)


def change_summary(diff: dict) -> str:
    """One-line human description of a bounded change."""
    return "; ".join(f"open.{k} changed" for k in diff) or "no change"
