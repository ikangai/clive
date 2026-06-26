"""Persistent learned-tool memo cache (gh#41 self-learning half).

The explorer captures ``ProbeOutcome``s only in memory; auto-explore's dedup
set is process-local. The same tool is therefore re-discovered from scratch
every run. This module persists a tiny best-effort JSON cache keyed by tool
name so a known-good invocation learned in one run can enrich the Tier-2 card
in the next (ExpeL/Voyager experiential reuse, arXiv 2308.10144).

Best-effort by design: every IO/JSON error is swallowed and logged. Recording
never raises to the caller (fire-and-forget); reading returns ``None`` on a
missing file, corrupt JSON, or an absent key.

Cache home is ``$CLIVE_HOME`` (else ``~/.clive``), read INSIDE the functions so
tests can redirect it to a tmp dir via monkeypatch. File: ``tool_memos.json``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_MEMO_FILE = "tool_memos.json"


def _cache_home() -> str:
    """Resolve the cache home dir. Read per-call so env overrides apply."""
    return os.environ.get("CLIVE_HOME") or os.path.expanduser("~/.clive")


def _memo_path() -> str:
    return os.path.join(_cache_home(), _MEMO_FILE)


def _load_all() -> dict:
    """Load the whole memo dict, or {} on missing file / corrupt JSON."""
    try:
        with open(_memo_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:  # ValueError covers JSONDecodeError
        log.debug("tool_memo: failed to load %s: %s", _memo_path(), exc)
        return {}


def record_tool_memo(tool_name: str, invocation: str, usage: str) -> None:
    """Persist a learned invocation for ``tool_name``. Never raises.

    Loads the existing dict, merges in this tool's entry, and writes atomically
    (tmp file + ``os.replace``). All IO/JSON errors are swallowed and logged.
    """
    try:
        memos = _load_all()
        memos[tool_name] = {"invocation": invocation, "usage": usage}

        home = _cache_home()
        os.makedirs(home, exist_ok=True)
        path = _memo_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(memos, fh)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError) as exc:
        log.debug("tool_memo: failed to record %r: %s", tool_name, exc)


def load_tool_memo(tool_name: str) -> Optional[dict]:
    """Return the memo dict for ``tool_name``, or None if absent/corrupt."""
    memo = _load_all().get(tool_name)
    return memo if isinstance(memo, dict) else None


def memo_card(tool_name: str) -> Optional[str]:
    """Compact ~1-line Tier-2 card synthesized from the memo, or None."""
    memo = load_tool_memo(tool_name)
    if not memo:
        return None
    invocation = memo.get("invocation", "")
    usage = memo.get("usage", "")
    return f"[{tool_name}] learned: `{invocation}` - {usage}"
