"""Prompts and safety lists for the discovery subsystem (gh#41)."""
from __future__ import annotations

from .models import ExplorationResult


# Tools that will trap exploration on a credential prompt if invoked
# without a help/version flag. explore_tool forces a --help suffix for
# these via _check_exploration_safety.
CREDENTIAL_TOOLS: frozenset[str] = frozenset({
    "aws", "gh", "gcloud", "az", "kubectl", "doctl",
    "psql", "mysql", "mongosh", "redis-cli",
    "ssh", "sftp", "scp", "rsync",
    "gpg", "pass", "op", "vault", "bw",
    "docker", "podman",
})

# Tools that drop into a full-screen TUI when launched without an arg.
# Same treatment as CREDENTIAL_TOOLS — force --help or refuse.
INTERACTIVE_TOOLS: frozenset[str] = frozenset({
    "vim", "vi", "nvim", "emacs", "nano",
    "less", "more", "most",
    "top", "htop", "btop", "iotop",
    "lazygit", "gitui", "tig",
    "k9s", "lazydocker",
    "ranger", "yazi", "mc", "nnn",
    "lynx", "w3m", "elinks",
    "mutt", "neomutt", "alpine",
    "tmux", "screen",
    "irssi", "weechat",
    "ncdu",
})


_DRIVER_TEMPLATE_HEADER = """\
You are synthesizing a clive driver file for the CLI tool `{tool}`.
You will be given an exploration history (each probe + its output).
Produce a single markdown file matching this exact shape:

```
---
preferred_mode: <script|interactive>
use_interactive_when: <one sentence>
agent_model: <fast|default>
observation_model: <fast|default>
---
# {tool} Driver

ENVIRONMENT: <one line>
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
- <command form 1>: <when to use>
- <command form 2>: <when to use>

PATTERNS:
- <pattern 1>
- <pattern 2>

PITFALLS:
- <pitfall 1>

RESPONSE FORMAT:
- <how the agent should respond when using this tool>

COMPLETION: DONE: <one-line summary>
```

Rules:
1. The output MUST start with `---` (frontmatter) and MUST contain ENVIRONMENT, PRIMARY TOOLS, PATTERNS, PITFALLS, RESPONSE FORMAT, and COMPLETION sections IN THAT ORDER. Each section name MUST appear as a heading-like line at the start of a line (not mentioned inside prose, not inside a fenced code block). Each section MUST appear exactly once.
2. Choose `preferred_mode: script` for batch tools (jq, rg, grep, curl); `preferred_mode: interactive` for TUI tools.
3. Be terse — reference-card-grade. No prose, no explanations.
4. Base every claim on what the exploration showed. If something is unknown, omit the bullet — do not invent. Use "PITFALLS: - none observed" if no pitfalls surfaced.
5. End with `COMPLETION: DONE: ...` — this is the literal signal the agent must emit.

Exploration history follows.
"""


def build_exploration_goal(tool_name: str) -> str:
    """The per-session goal prepended to the initial user message."""
    return (
        f"Explore the CLI tool `{tool_name}`. Follow the PROBE ORDER in your driver. "
        f"Run `{tool_name} --help` first, then iterate. Do NOT run destructive "
        f"commands (rm, dd, chmod, etc). Stay read-only. After 5-8 probes, DONE: "
        f"with a one-line summary of what the tool does."
    )


def build_generation_prompt(result: ExplorationResult) -> str:
    """Build the LLM prompt that synthesizes a driver from an ExplorationResult."""
    header = _DRIVER_TEMPLATE_HEADER.format(tool=result.tool_name)
    lines = [header, "", f"Tool: {result.tool_name}", ""]
    if result.summary:
        lines.append(f"Exploration summary: {result.summary}")
        lines.append("")
    lines.append("Probes:")
    for i, p in enumerate(result.probes, 1):
        status = "OK" if p.success else f"FAIL(exit={p.exit_code})"
        lines.append(f"  [{i}] [{status}] {p.command}")
        screen_head = "\n".join(p.screen.splitlines()[:12])
        for sl in screen_head.splitlines():
            lines.append(f"      {sl}")
        lines.append("")
    return "\n".join(lines)
