"""Translate a harness spec's `open` block into clive's real runtime knobs.

This is the bridge that makes a candidate spec MEASURABLE without editing clive's
source: `open` fields render to clive's existing env vars + CLI flags. The model
is NOT set here — it comes from the panel (the same candidate is run under each
panel model). Fields clive cannot actuate today are recorded in `pending` and
surfaced on the run record — never silently dropped (no silent no-op).

Grounded knobs (discovery):
  system_prompt              -> CLIVE_EVAL_DRIVER_OVERRIDE=<file> (global driver override)
  command_affordances.toolset-> -t <spec> + CLIVE_TOOLSET
  command_affordances.progressive_disclosure -> CLIVE_PROGRESSIVE_TOOLS
  observation_policy.streaming      -> CLIVE_STREAMING_OBS
  observation_policy.control_sidecar-> CLIVE_CONTROL_SIDECAR
  observation_policy.speculate      -> CLIVE_SPECULATE
  observation_policy.pane_isolation -> CLIVE_PANE_ISOLATION
  observation_policy.ps1_exitcode   -> CLIVE_PS1_EXITCODE
  recovery_policy.max_turns         -> (source constant _DEFAULT_MAX_TURNS) -> pending
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _flag(b: Any) -> str:
    return "1" if b else "0"


@dataclass
class AppliedSpec:
    env: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)   # declared but not actuatable
    notes: list[str] = field(default_factory=list)


def apply_spec(spec: dict, run_dir: str, default_toolset: str = "minimal") -> AppliedSpec:
    out = AppliedSpec()
    open_block = spec.get("open", {}) or {}

    # --- system_prompt -> global driver override --------------------------
    sysp = open_block.get("system_prompt")
    if isinstance(sysp, str) and sysp.strip():
        override = os.path.join(run_dir, "driver_override.md")
        with open(override, "w", encoding="utf-8") as fh:
            fh.write(sysp)
        out.env["CLIVE_EVAL_DRIVER_OVERRIDE"] = override
        out.notes.append("system_prompt actuated via CLIVE_EVAL_DRIVER_OVERRIDE")

    # --- command_affordances ----------------------------------------------
    aff = open_block.get("command_affordances")
    toolset = default_toolset
    if isinstance(aff, dict):
        toolset = aff.get("toolset", default_toolset) or default_toolset
        if aff.get("progressive_disclosure"):
            out.env["CLIVE_PROGRESSIVE_TOOLS"] = "1"
    out.flags += ["-t", toolset]
    out.env["CLIVE_TOOLSET"] = toolset

    # --- observation_policy ------------------------------------------------
    obs = open_block.get("observation_policy")
    if isinstance(obs, dict):
        if "streaming" in obs:
            out.env["CLIVE_STREAMING_OBS"] = _flag(obs.get("streaming"))
        if "control_sidecar" in obs:
            out.env["CLIVE_CONTROL_SIDECAR"] = _flag(obs.get("control_sidecar"))
        if "speculate" in obs:
            out.env["CLIVE_SPECULATE"] = _flag(obs.get("speculate"))
        if "pane_isolation" in obs:
            out.env["CLIVE_PANE_ISOLATION"] = _flag(obs.get("pane_isolation"))
        if "ps1_exitcode" in obs:
            out.env["CLIVE_PS1_EXITCODE"] = _flag(obs.get("ps1_exitcode"))

    # --- recovery_policy ---------------------------------------------------
    rec = open_block.get("recovery_policy")
    if isinstance(rec, dict):
        # max_turns is a source constant (_DEFAULT_MAX_TURNS=4) — not env-actuatable
        # in Phase 0. Record it as pending rather than pretend it took effect.
        if "max_turns" in rec:
            out.pending.append(
                f"recovery_policy.max_turns={rec['max_turns']} "
                f"(clive uses source constant _DEFAULT_MAX_TURNS; actuation pending)")
        for k in ("script_to_interactive_fallback", "backoff", "idempotency"):
            if k in rec:
                out.pending.append(f"recovery_policy.{k}={rec[k]} (actuation pending)")

    # --- skills ------------------------------------------------------------
    skills = open_block.get("skills")
    if isinstance(skills, list) and skills:
        # Availability is governed by the toolset; the clive-to-clive comms skill
        # ('clive-rooms') is actuated by the multi-clive runner path, not an env
        # var. Record for traceability.
        names = [s.get("name") if isinstance(s, dict) else s for s in skills]
        out.notes.append("skills declared: " + ", ".join(str(n) for n in names))

    return out
