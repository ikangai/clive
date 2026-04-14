"""Append-only audit trail for self-modification attempts."""

import hashlib
import json
import os
import time
from pathlib import Path

AUDIT_DIR = Path(__file__).resolve().parent.parent / ".clive" / "audit"


def _ensure_dir() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def log_attempt(
    proposal_id: str,
    action: str,
    files: list[str],
    tier: str,
    roles: dict[str, str],
    gate_result: dict,
    outcome: str,
    details: str = "",
) -> Path:
    """Log a modification attempt. Returns path to the log entry."""
    _ensure_dir()

    entry = {
        "timestamp": time.time(),
        "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "proposal_id": proposal_id,
        "action": action,
        "files": files,
        "tier": tier,
        "roles": roles,
        "gate_result": gate_result,
        "outcome": outcome,
        "details": details,
    }

    # Compute integrity hash (chain with previous entry)
    prev_hash = _get_last_hash()
    entry["prev_hash"] = prev_hash
    payload = json.dumps(entry, sort_keys=True)
    entry["hash"] = hashlib.sha256(payload.encode()).hexdigest()

    filename = f"{int(time.time())}_{proposal_id}.json"
    path = AUDIT_DIR / filename

    with open(path, "w") as f:
        json.dump(entry, f, indent=2)
        f.write("\n")

    return path


def _get_last_hash() -> str:
    """Get hash of the most recent audit entry (chain integrity)."""
    _ensure_dir()
    entries = sorted(AUDIT_DIR.glob("*.json"))
    if not entries:
        return "genesis"
    try:
        with open(entries[-1]) as f:
            data = json.load(f)
        return data.get("hash", "unknown")
    except (json.JSONDecodeError, KeyError):
        return "corrupted"


def get_session_count() -> int:
    """Count modification attempts in this session (since process start)."""
    _ensure_dir()
    import selfmod

    start_time = getattr(selfmod, "_session_start", None)
    if start_time is None:
        selfmod._session_start = time.time()
        start_time = selfmod._session_start

    count = 0
    for entry_path in AUDIT_DIR.glob("*.json"):
        try:
            with open(entry_path) as f:
                data = json.load(f)
            if data.get("timestamp", 0) >= start_time:
                count += 1
        except (json.JSONDecodeError, KeyError):
            continue
    return count


def verify_chain() -> tuple[bool, list[str]]:
    """Verify the integrity of the audit chain. Returns (valid, errors)."""
    _ensure_dir()
    entries = sorted(AUDIT_DIR.glob("*.json"))
    errors = []
    prev_hash = "genesis"

    for entry_path in entries:
        try:
            with open(entry_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, KeyError) as e:
            errors.append(f"{entry_path.name}: parse error: {e}")
            continue

        if data.get("prev_hash") != prev_hash:
            errors.append(
                f"{entry_path.name}: chain break: expected {prev_hash}, "
                f"got {data.get('prev_hash')}"
            )

        # Verify self-hash
        stored_hash = data.pop("hash", None)
        payload = json.dumps(data, sort_keys=True)
        computed = hashlib.sha256(payload.encode()).hexdigest()
        data["hash"] = stored_hash  # restore

        if stored_hash != computed:
            errors.append(f"{entry_path.name}: hash mismatch")

        prev_hash = stored_hash or "unknown"

    return len(errors) == 0, errors
