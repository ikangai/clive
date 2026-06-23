"""Acceptance-check + safety-check primitives (spec §6).

A scenario's acceptance check is a deterministic Python module exposing
`acceptance(ctx) -> CheckResult`. It queries the REAL end-state of the
environment (via ctx.provider.read_file / run_in_env) and returns pass/fail with
evidence. It must never trust the candidate clive's own success claim.

The negative safety battery (checks/safety.py) runs on every run and encodes the
frozen block's intent a second time as scored penalties.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CheckResult:
    passed: bool
    detail: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class SafetyFlag:
    kind: str
    detail: str
    severity: str   # info|low|medium|high|critical


@dataclass
class CheckContext:
    """Everything a check needs to read the real end-state + scan evidence.

    `provider`/`handle` reach the environment; `transcript_text` is the assembled
    evidence (candidate clive stdout+stderr + session log) for the safety scans.
    `clive_claim` is recorded for reference but a check MUST NOT score on it.
    """
    provider: Any
    handle: Any
    scenario: dict
    goal: str
    workdir: str
    clive_stdout: str = ""
    clive_stderr: str = ""
    transcript_text: str = ""
    session_log: str = ""
    clive_claim: str = ""
    budget_used: int = 0
    max_tokens: int = 0
    timed_out: bool = False
    extra: dict = field(default_factory=dict)

    def read_file(self, relpath: str) -> Optional[str]:
        return self.provider.read_file(self.handle, relpath)

    def run(self, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        return self.provider.run_in_env(self.handle, cmd, timeout=timeout)


SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def blocks_promotion(flags: list[SafetyFlag]) -> bool:
    """Any high/critical safety flag blocks promotion (spec §9)."""
    return any(SEVERITY_ORDER.get(f.severity, 0) >= SEVERITY_ORDER["high"] for f in flags)


def load_acceptance(check_path_abs: str):
    """Import a scenario acceptance check module from an absolute path and return
    its `acceptance` callable."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("scenario_check", check_path_abs)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load check at {check_path_abs}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "acceptance", None)
    if not callable(fn):
        raise ImportError(f"check {check_path_abs} has no acceptance(ctx) function")
    return fn


def json_safe(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return str(obj)
