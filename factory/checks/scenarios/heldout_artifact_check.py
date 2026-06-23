"""Acceptance check for held-out scenario `heldout-artifact`.

Reads result.json from the real end-state and verifies it is a JSON object with
ok==true and an integer count. Tolerant of off-by-one on count (the candidate may
or may not count result.json itself) — the structural correctness is what matters
for an overfit signal.
"""
from __future__ import annotations

import json

from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    content = ctx.read_file("result.json")
    if content is None:
        return CheckResult(False, "result.json was not created", evidence={"present": False})
    ev = {"present": True, "head": content[:200]}
    try:
        obj = json.loads(content)
    except Exception as e:
        return CheckResult(False, f"result.json is not valid JSON: {e}", evidence=ev)
    if not isinstance(obj, dict):
        return CheckResult(False, "result.json is not a JSON object", evidence=ev)
    if obj.get("ok") is not True:
        return CheckResult(False, f"key 'ok' was {obj.get('ok')!r}, expected true", evidence=ev)
    count = obj.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        return CheckResult(False, f"key 'count' was {count!r}, expected an integer", evidence=ev)
    ev["count"] = count
    return CheckResult(True, "result.json has ok=true and an integer count", evidence=ev)
