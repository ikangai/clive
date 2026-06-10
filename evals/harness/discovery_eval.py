"""Layer 5 tool-discovery eval support (gh#40).

Layer 5 evals test the gh#39 progressive-disclosure registry: the agent
starts with only the Tier 0 category index and must navigate to the right
tool via the in-pane ``clive-tools`` CLI (Tier 1 names, Tier 2 cards)
before solving the task.

Three pieces live here:

- :func:`build_discovery_context` — the dep_context injected into the
  worker so it starts at the configured registry tier.
- :func:`check_discovery_criteria` — post-hoc verification of the
  *process* (did the agent use discovery? did it pick the expected
  tool?) against the pane scrollback. Command lines are identified by
  the eval fixture's PS1 marker; output lines never count as usage, so
  a tool name appearing in a ``clive-tools`` listing is not a match.
  Only the first line of a multi-line command is visible this way —
  task checks should not depend on heredoc bodies.
- :func:`make_disabled_tool_shims` — a PATH-prepended shim dir whose
  entries exit 127, used by fallback evals to make a tool unavailable
  even when installed on the host.
"""
from __future__ import annotations

import os
import re
import stat

# Must match the PS1 exported by EvalFixture.__enter__.
PROMPT_MARKER = "[AGENT_READY] $"

_SHIM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")

# gh#40 spells the discovery commands "tools"/"tool_info"; the shipped
# CLI is `clive-tools list` / `clive-tools info`. Accept both spellings.
_COMMAND_PATTERNS = {
    "list": re.compile(r"clive-tools(\s+list\b|\s*$)"),
    "tools": re.compile(r"clive-tools(\s+list\b|\s*$)"),
    "info": re.compile(r"clive-tools\s+info\s+\S+"),
    "tool_info": re.compile(r"clive-tools\s+info\s+\S+"),
}

_CLIVE_TOOLS_RE = re.compile(r"(^|[/\s])clive-tools\b")


def build_discovery_context(registry_tier: int = 0) -> str:
    """Build the worker dep_context for a discovery eval.

    Tier 0: category index only — the agent must drill down itself.
    Tier 1: category index + tool names per category.
    """
    if registry_tier not in (0, 1):
        raise ValueError(f"registry_tier must be 0 or 1, got {registry_tier}")

    from toolsets import CATEGORIES, build_tier0_summary, build_tier1_names

    categories = list(CATEGORIES.keys())
    parts = [
        "TOOL DISCOVERY: you do not have a tool list. "
        "Discover tools before using them:",
        build_tier0_summary(categories),
    ]
    if registry_tier >= 1:
        parts.append(build_tier1_names(categories))
    parts.append(
        "Use `clive-tools list` for the category index, "
        "`clive-tools list <category>` for tool names, and "
        "`clive-tools info <tool>` for a usage card before running a tool."
    )
    return "\n\n".join(parts)


def make_disabled_tool_shims(shim_dir: str, disabled: list[str]) -> str | None:
    """Create exit-127 shims for ``disabled`` tools; return the dir to
    prepend to PATH, or None when there is nothing to disable."""
    if not disabled:
        return None
    for name in disabled:
        if not _SHIM_NAME_RE.match(name or ""):
            raise ValueError(f"invalid tool name for shim: {name!r}")
    os.makedirs(shim_dir, exist_ok=True)
    for name in disabled:
        path = os.path.join(shim_dir, name)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\necho "{name}: command not found" >&2\nexit 127\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim_dir


def _command_lines(scrollback: str) -> list[str]:
    """Extract typed commands from scrollback via the PS1 marker."""
    cmds = []
    for line in scrollback.splitlines():
        if PROMPT_MARKER in line:
            cmd = line.split(PROMPT_MARKER, 1)[1].strip()
            if cmd:
                cmds.append(cmd)
    return cmds


def _tool_in_command(name: str, cmd: str) -> bool:
    """Tool name appears in command position-ish: word-delimited, not
    embedded in a larger token like result.txt or http://."""
    return re.search(
        rf"(^|[|;&(\s/]){re.escape(name)}(\s|$)", cmd
    ) is not None


def _match_alternation(pattern: str, cmds: list[str]) -> str | None:
    """Return the first alternative from 'a|b|c' used in any command."""
    for name in pattern.split("|"):
        name = name.strip()
        if name and any(_tool_in_command(name, c) for c in cmds):
            return name
    return None


def check_discovery_criteria(
    criteria: dict, scrollback: str, script_text: str = ""
) -> tuple[bool, dict, str]:
    """Check a task's discovery_criteria against the available evidence.

    Evidence is pane scrollback (interactive mode: commands typed at the
    PS1 marker) plus, for script mode, the generated script's own lines —
    script_runner executes `bash /tmp/clive/_script_<id>.sh`, so the
    tools chained in a pipeline only appear inside the script file.

    Returns (ok, fields, detail) where fields holds the ToolEvalResult
    extras (tool_used, tool_expected, tool_correct, discovery_turns,
    fallback_used, fallback_expected, pipeline_stages).
    """
    cmds = _command_lines(scrollback)
    if script_text:
        cmds += [
            line.strip()
            for line in script_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    problems: list[str] = []

    fields: dict = {
        "tool_used": None,
        "tool_expected": criteria.get("expected_tool"),
        "tool_correct": True,
        "discovery_turns": sum(1 for c in cmds if _CLIVE_TOOLS_RE.search(c)),
        "fallback_used": False,
        "fallback_expected": "expected_fallback" in criteria,
        "pipeline_stages": 0,
    }

    for want in criteria.get("must_use_commands", []):
        pat = _COMMAND_PATTERNS.get(want)
        if pat is None:
            problems.append(f"unknown must_use command: {want}")
        elif not any(pat.search(c) for c in cmds):
            problems.append(f"discovery command not used: {want}")

    if "expected_tool" in criteria:
        used = _match_alternation(criteria["expected_tool"], cmds)
        fields["tool_used"] = used
        if used is None:
            fields["tool_correct"] = False
            problems.append(f"expected tool not used: {criteria['expected_tool']}")

    if "expected_tools" in criteria:
        matched = 0
        for pattern in criteria["expected_tools"]:
            used = _match_alternation(pattern, cmds)
            if used is None:
                fields["tool_correct"] = False
                problems.append(f"expected tool not used: {pattern}")
            else:
                matched += 1
                if fields["tool_used"] is None:
                    fields["tool_used"] = used
        fields["pipeline_stages"] = matched

    if "expected_fallback" in criteria:
        used = _match_alternation(criteria["expected_fallback"], cmds)
        if used is None:
            problems.append(
                f"expected fallback not used: {criteria['expected_fallback']}"
            )
        else:
            fields["fallback_used"] = True
            if fields["tool_used"] is None:
                fields["tool_used"] = used

    ok = not problems
    detail = "discovery criteria passed" if ok else "; ".join(problems)
    return ok, fields, detail
