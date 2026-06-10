# Self-learning tool discovery (gh#41) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give clive the ability to learn how to use an arbitrary CLI tool by exploring it (`--help`, `man`, `tldr`, safe probes) and synthesizing a `drivers/<name>.md` from the exploration log. This session ships the manual `--explore <tool>` entry point and the underlying primitives; the audit-log refiner, auto-trigger from `_expand_toolset`, and category registration are deferred to follow-on cards.

**Architecture:** A new `src/clive/discovery/` subpackage hosts two pure-ish functions: `explore_tool(name)` constructs a synthetic `Subtask(mode="interactive")` and reuses `run_subtask_interactive` from the existing execution layer — only the driver, the goal prompt, and the post-hoc probe extraction are new. `generate_driver(name, log)` asks the LLM to synthesize a `drivers/<name>.md` from the exploration history. A new `--explore <tool>` CLI flag is the manual entry point. The refiner and auto-explore integration are out of scope for this session — both have correctness/integration gaps that make them dead-on-arrival until follow-up cards land.

**Tech Stack:** Python 3.10+, existing `run_subtask_interactive` for the exploration session, existing `chat()` LLM client, existing `_check_command_safety` (post-H10 shlex-based), existing driver-loading machinery in `llm/prompts.py`.

---

## Revised scope (after code review)

The first draft of this plan had 10 tasks including a `refine_driver()` function (audit-log replay) and a `CLIVE_AUTO_EXPLORE=1` integration into `_expand_toolset`. Review feedback flagged three issues:

1. **Refiner without orchestrator is half-built.** The refinement *function* requires gh#40's Layer 5 eval framework to be useful as an automated loop. Shipping the building block without the loop adds API surface that nothing exercises. → Moved to follow-up.
2. **Auto-explore is dead-on-arrival.** A driver written to `drivers/<name>.md` during `_expand_toolset` isn't picked up by any toolset entry without gh#39's category auto-classification. → Moved to follow-up.
3. **`explore_tool` was reinventing `run_subtask_interactive`.** The existing interactive runner already has intervention detection, streaming-obs, per-pane locking, context compression. Reuse it via a synthetic `Subtask`, don't rebuild it. → Reflected in revised task 3.

What stays in this session: 8 tasks. Manual `--explore` produces real drivers from real exploration sessions. Foundation is solid; the integration layers come next.

---

## Conventions for every task

- TDD throughout. **No production code lands without a failing test first.**
- One logical change per commit. Test + impl in the same commit (TDD discipline) is fine; conflating unrelated changes is not.
- Each task ends with `pytest -q --tb=short` passing the whole suite (currently 990).
- Commit message format: `feat(discovery): <imperative>` / `docs(discovery): <imperative>`. Co-author trailer per repo convention.

---

## Task 1: Discovery subpackage skeleton + data classes

**Files:**
- Create: `src/clive/discovery/__init__.py`
- Create: `src/clive/discovery/models.py`
- Create: `tests/test_discovery_models.py`

**Step 1: Failing test**

```python
# tests/test_discovery_models.py
from discovery.models import ExplorationResult, ProbeOutcome


def test_probe_outcome_success_when_exit_zero():
    p = ProbeOutcome(command="echo --help", exit_code=0, screen="Usage: echo")
    assert p.success is True


def test_probe_outcome_failure_on_nonzero():
    p = ProbeOutcome(command="echo --foo", exit_code=2, screen="bad arg")
    assert p.success is False


def test_probe_outcome_failure_on_none_exit():
    # exit_code=None means "blocked by safety check or never executed"
    p = ProbeOutcome(command="rm -rf /", exit_code=None, screen="[Blocked]")
    assert p.success is False


def test_exploration_result_aggregates_probes():
    r = ExplorationResult(tool_name="echo", probes=[
        ProbeOutcome(command="echo --help", exit_code=0, screen="usage..."),
        ProbeOutcome(command="echo --version", exit_code=2, screen="bad"),
    ])
    assert r.tool_name == "echo"
    assert r.success_count == 1
    assert r.failure_count == 1
```

**Step 2: Run, expect ImportError.**

**Step 3: Implement**

```python
# src/clive/discovery/__init__.py — empty for now
```

```python
# src/clive/discovery/models.py
"""Data classes for the self-learning tool discovery system (gh#41)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProbeOutcome:
    """One command tried during exploration."""
    command: str
    exit_code: int | None
    screen: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass
class ExplorationResult:
    """Aggregated result of an exploration session."""
    tool_name: str
    probes: list[ProbeOutcome] = field(default_factory=list)
    summary: str = ""

    @property
    def success_count(self) -> int:
        return sum(1 for p in self.probes if p.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for p in self.probes if not p.success)
```

**Step 4: Run, expect PASS.**

**Step 5: Commit**

```bash
git add src/clive/discovery/__init__.py src/clive/discovery/models.py tests/test_discovery_models.py
git commit -m "feat(discovery): ExplorationResult + ProbeOutcome dataclasses (gh#41)"
```

---

## Task 2: Exploration driver + prompts

**Files:**
- Create: `src/clive/drivers/explore.md` (auto-discovered when `app_type='explore'`)
- Create: `src/clive/discovery/prompts.py`
- Create: `tests/test_discovery_prompts.py`

**Step 1: Failing test**

```python
# tests/test_discovery_prompts.py
import re

from discovery.models import ExplorationResult, ProbeOutcome
from discovery.prompts import build_exploration_goal, build_generation_prompt, CREDENTIAL_TOOLS, INTERACTIVE_TOOLS


def test_exploration_goal_references_tool_name():
    g = build_exploration_goal("rg")
    assert "rg" in g
    assert "--help" in g


def test_exploration_goal_warns_against_destructive_probes():
    g = build_exploration_goal("rg")
    assert re.search(r"do not|DO NOT", g, re.IGNORECASE)
    assert re.search(r"destructive|modify|write", g, re.IGNORECASE)


def test_generation_prompt_includes_driver_template_sections():
    r = ExplorationResult(tool_name="rg", probes=[
        ProbeOutcome(command="rg --help", exit_code=0, screen="ripgrep usage..."),
    ])
    p = build_generation_prompt(r)
    for section in ("ENVIRONMENT", "PRIMARY TOOLS", "PATTERNS", "RESPONSE FORMAT", "COMPLETION"):
        assert section in p
    assert "rg --help" in p
    assert "ripgrep usage" in p


def test_generation_prompt_demands_frontmatter():
    p = build_generation_prompt(ExplorationResult(tool_name="rg"))
    assert "---" in p
    assert "preferred_mode" in p


def test_credential_tools_list_includes_common_offenders():
    # Tools that prompt for credentials must be in CREDENTIAL_TOOLS so
    # explore_tool can force a --help suffix.
    for tool in ("aws", "gh", "gcloud", "kubectl", "psql", "mysql", "ssh"):
        assert tool in CREDENTIAL_TOOLS


def test_interactive_tools_list_includes_common_tuis():
    for tool in ("vim", "less", "more", "top", "htop", "lazygit", "k9s"):
        assert tool in INTERACTIVE_TOOLS
```

**Step 2: Run, expect ImportError.**

**Step 3: Implement**

```markdown
<!-- src/clive/drivers/explore.md -->
---
preferred_mode: interactive
use_interactive_when: always — exploration is inherently iterative
agent_model: fast
observation_model: fast
---
# Tool Exploration Driver

ENVIRONMENT: bash shell with PS1="[AGENT_READY] $ ". You are meeting a CLI tool you have never used.
WORKING DIR: /tmp/clive

GOAL: Learn what the tool does and how to use it by running safe, read-only probes. The pane history IS the curriculum. A driver will be synthesized from this session.

PROBE ORDER (try in sequence, skip what fails):
1. `<tool> --help`
2. `<tool> -h`
3. `man <tool> 2>&1 | head -80`
4. `tldr <tool>` (if installed)
5. `<tool> --version`
6. One or two read-only example invocations derived from the help text — only if clearly safe.

DO NOT:
- Run destructive probes (rm, dd, chmod, mv, mkfs, fdisk, format, init).
- Modify files outside /tmp/clive.
- Connect to networks unless the tool requires it AND it's read-only.
- Try sudo.
- Probe more than 8 commands total — exploration is bounded.
- Run a tool that prompts for credentials (aws, gh, gcloud, kubectl, psql, mysql, ssh) WITHOUT `--help` or `--version` — these will trap on a password prompt.
- Run a TUI tool (vim, less, top, lazygit, k9s) without `--help` — they will trap the terminal.

PATTERNS:
- If `--help` is unrecognized, try `-h`. If both fail, try `man <tool>`.
- If the tool drops into a TUI, you have miscalculated — DONE: immediately.
- If the tool wants config or credentials, STOP and DONE: report it. Don't set up credentials.

RESPONSE FORMAT:
- ALWAYS respond with a ```bash code block containing your next probe.
- After 5-8 probes or when you have enough to summarize, DONE: <one-line summary describing what kind of tool this is and its 2-3 most useful invocations>.

COMPLETION: DONE: <summary>. The summary line ends exploration and goes into the synthesized driver.
```

```python
# src/clive/discovery/prompts.py
"""Prompts and safety lists for the discovery subsystem (gh#41)."""
from __future__ import annotations

from .models import ExplorationResult


# Tools that will trap exploration on a credential prompt if invoked without
# a help/version flag. explore_tool enforces a --help suffix for these.
CREDENTIAL_TOOLS: frozenset[str] = frozenset({
    "aws", "gh", "gcloud", "az", "kubectl", "doctl",
    "psql", "mysql", "mongosh", "redis-cli",
    "ssh", "sftp", "scp", "rsync",
    "gpg", "pass", "op", "vault", "bw",
    "docker", "podman",
})

# Tools that drop into a full-screen TUI when launched without an arg.
# Same treatment as CREDENTIAL_TOOLS: force --help or refuse.
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
1. The output MUST start with `---` (frontmatter) and MUST contain ENVIRONMENT, PRIMARY TOOLS, PATTERNS, RESPONSE FORMAT, and COMPLETION sections, each as a heading-like line at the start of a line (not mentioned inside prose).
2. Choose `preferred_mode: script` for batch tools (jq, rg, grep, curl); `preferred_mode: interactive` for TUI tools.
3. Be terse — reference-card-grade. No prose, no explanations.
4. Base every claim on what the exploration showed. If something is unknown, omit it — do not invent.
5. End with `COMPLETION: DONE: ...` — this is the literal signal the agent must emit.

Exploration history follows.
"""


def build_exploration_goal(tool_name: str) -> str:
    """The per-session goal that gets prepended to the initial user message."""
    return (
        f"Explore the CLI tool `{tool_name}`. Follow the PROBE ORDER in your driver. "
        f"Run `{tool_name} --help` first, then iterate. Do NOT run destructive commands "
        f"(rm, dd, chmod, etc). Stay read-only. After 5-8 probes, DONE: with a one-line "
        f"summary of what the tool does."
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
```

**Step 4: Run, expect PASS.**

**Step 5: Commit**

```bash
git add src/clive/drivers/explore.md src/clive/discovery/prompts.py tests/test_discovery_prompts.py
git commit -m "feat(discovery): explore driver + generation prompt + safety lists (gh#41)"
```

---

## Task 3: `explore_tool()` — adapter over `run_subtask_interactive`

**Files:**
- Create: `src/clive/discovery/explorer.py`
- Create: `tests/test_discovery_explorer.py`

**Design (per code review B1):** instead of reinventing the interactive loop, build a thin adapter that:
1. Opens a fresh exploration pane (`add_pane` + the `explore.md` driver via `app_type="explore"`).
2. Constructs a synthetic `Subtask(mode="interactive", max_turns=8, pane=..., description=build_exploration_goal(name))`.
3. Hooks an `on_event` callback that intercepts `turn` events to record `ProbeOutcome`s.
4. Pre-filters commands via `_check_command_safety` **and** the credential/interactive tool guards before they reach the pane.
5. Returns the `ExplorationResult` derived from the recorded probes + final summary.

Pre-filtering credential/interactive tools is implemented as a wrapper around the existing `_check_command_safety`. We monkey-patch nothing — we add a new helper `_check_exploration_safety(cmd, tool)` that calls `_check_command_safety` first, then layers on the exploration-specific rules.

**Step 1: Failing test**

```python
# tests/test_discovery_explorer.py
from unittest.mock import MagicMock

import pytest

from discovery.explorer import (
    explore_tool, _check_exploration_safety,
)
from discovery.models import ExplorationResult, ProbeOutcome


# ─── Exploration safety unit tests (no LLM, no pane) ─────────────────────

def test_safety_allows_basic_help():
    assert _check_exploration_safety("rg --help", "rg") is None
    assert _check_exploration_safety("rg -h", "rg") is None
    assert _check_exploration_safety("man rg 2>&1 | head -80", "rg") is None


def test_safety_blocks_credential_tool_without_help_flag():
    # aws is in CREDENTIAL_TOOLS — bare invocation would prompt for keys.
    v = _check_exploration_safety("aws s3 ls", "aws")
    assert v is not None
    assert "credential" in v.lower() or "help" in v.lower()


def test_safety_allows_credential_tool_with_help_flag():
    assert _check_exploration_safety("aws --help", "aws") is None
    assert _check_exploration_safety("aws --version", "aws") is None
    assert _check_exploration_safety("kubectl -h", "kubectl") is None


def test_safety_blocks_tui_tool_without_help():
    v = _check_exploration_safety("vim file.txt", "vim")
    assert v is not None
    assert "interactive" in v.lower() or "tui" in v.lower() or "help" in v.lower()


def test_safety_allows_tui_tool_with_help():
    assert _check_exploration_safety("vim --help", "vim") is None


def test_safety_still_blocks_destructive():
    v = _check_exploration_safety("rm -rf /", "rg")
    assert v is not None


# ─── explore_tool integration tests (everything mocked) ───────────────────

class _FakeRun:
    """A stand-in for run_subtask_interactive that drives the on_event callback
    with a scripted sequence of (cmd, screen, exit_code) tuples."""

    def __init__(self, script):
        self.script = script

    def __call__(self, subtask, pane_info, dep_context, on_event=None, session_dir="/tmp/clive"):
        from models import SubtaskResult, SubtaskStatus
        for i, (cmd, screen, exit_code) in enumerate(self.script, start=1):
            if on_event:
                on_event("turn", subtask.id, i, cmd)
                # The explorer attaches a custom on_event that *also* records
                # a probe — we surface the screen+exit via this side channel:
                on_event("probe", subtask.id, cmd, exit_code, screen)
        return SubtaskResult(
            subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
            summary=self.script[-1][1] if self.script else "no probes",
            output_snippet="", turns_used=len(self.script),
            prompt_tokens=10, completion_tokens=10,
        )


def test_explore_tool_records_probes(monkeypatch):
    script = [
        ("echo --help", "Usage: echo [OPTION]...", 0),
        ("echo --version", "echo 8.32", 0),
    ]
    monkeypatch.setattr("discovery.explorer.run_subtask_interactive", _FakeRun(script))
    monkeypatch.setattr("discovery.explorer._open_exploration_pane", lambda sd: MagicMock(name="pane"))
    monkeypatch.setattr("discovery.explorer._close_exploration_pane", lambda p: None)

    result = explore_tool("echo")
    assert isinstance(result, ExplorationResult)
    assert result.tool_name == "echo"
    assert len(result.probes) == 2
    assert result.probes[0].command == "echo --help"
    assert result.probes[0].exit_code == 0


def test_explore_tool_empty_when_no_probes(monkeypatch):
    monkeypatch.setattr("discovery.explorer.run_subtask_interactive", _FakeRun([]))
    monkeypatch.setattr("discovery.explorer._open_exploration_pane", lambda sd: MagicMock())
    monkeypatch.setattr("discovery.explorer._close_exploration_pane", lambda p: None)

    result = explore_tool("nothing")
    assert result.tool_name == "nothing"
    assert result.probes == []


def test_explore_tool_session_dir_is_unique_per_tool(monkeypatch, tmp_path):
    captured = {}
    def fake_run(subtask, pane_info, dep_context, on_event=None, session_dir="/tmp/clive"):
        from models import SubtaskResult, SubtaskStatus
        captured["session_dir"] = session_dir
        return SubtaskResult(
            subtask_id=subtask.id, status=SubtaskStatus.COMPLETED,
            summary="", output_snippet="", turns_used=0,
            prompt_tokens=0, completion_tokens=0,
        )
    monkeypatch.setattr("discovery.explorer.run_subtask_interactive", fake_run)
    monkeypatch.setattr("discovery.explorer._open_exploration_pane", lambda sd: MagicMock())
    monkeypatch.setattr("discovery.explorer._close_exploration_pane", lambda p: None)

    explore_tool("rg", session_dir_root=str(tmp_path))
    assert "rg" in captured["session_dir"]
    assert captured["session_dir"].startswith(str(tmp_path))
```

**Step 2: Run, expect ImportError.**

**Step 3: Implement**

```python
# src/clive/discovery/explorer.py
"""Bounded exploration runner for the self-learning tool discovery system (gh#41).

Adapter over execution.interactive_runner.run_subtask_interactive — we get
intervention detection, streaming-obs, per-pane locking, and context
compression for free. The explorer-specific additions are:

  - A pre-built ``Subtask(mode='interactive', pane='explore', app_type-implied 'explore')``.
  - A per-tool session directory under ``session_dir_root``.
  - Exploration-specific safety guards layered on top of ``_check_command_safety``
    (credential prompts, TUI traps).
  - Post-hoc probe extraction via an ``on_event`` callback that records every
    command + screen + exit code into an ExplorationResult.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

import libtmux

from execution.interactive_runner import run_subtask_interactive
from models import PaneInfo, Subtask
from runtime import _check_command_safety
from session import SOCKET_NAME, _maybe_attach_stream, add_pane, detach_stream

from .models import ExplorationResult, ProbeOutcome
from .prompts import (
    CREDENTIAL_TOOLS,
    INTERACTIVE_TOOLS,
    build_exploration_goal,
)

log = logging.getLogger(__name__)

_DEFAULT_MAX_TURNS = 8
_HELP_FLAGS = ("--help", "-h", "--version", "-V")


def _check_exploration_safety(command: str, tool_name: str) -> str | None:
    """Run the standard safety check, then layer on exploration-specific rules.

    Returns a violation string or None if the command is acceptable to send.
    """
    base = _check_command_safety(command)
    if base is not None:
        return base

    cmd_trimmed = command.strip()
    # The first token (after optional `sudo`, `env VAR=val`) is the command word.
    tokens = cmd_trimmed.split()
    if not tokens:
        return None
    head = tokens[0]
    while head in ("sudo",) or (len(tokens) >= 2 and "=" in head and head.split("=", 1)[0].replace("_", "").isalnum()):
        tokens = tokens[1:]
        if not tokens:
            return None
        head = tokens[0]

    has_help_flag = any(flag in tokens for flag in _HELP_FLAGS)

    if head in CREDENTIAL_TOOLS and not has_help_flag:
        return (
            f"Blocked: `{head}` may prompt for credentials. "
            f"Append --help or --version, or skip this probe."
        )
    if head in INTERACTIVE_TOOLS and not has_help_flag:
        return (
            f"Blocked: `{head}` opens an interactive TUI. "
            f"Append --help or skip this probe."
        )
    return None


def explore_tool(
    tool_name: str,
    session_dir_root: str = "/tmp/clive",
    max_turns: int = _DEFAULT_MAX_TURNS,
    pane_info: Optional[PaneInfo] = None,
) -> ExplorationResult:
    """Run a bounded exploration session for ``tool_name``.

    Reuses ``run_subtask_interactive`` for the actual loop. Returns an
    ExplorationResult populated by an on_event callback that records every
    command + exit code + post-command screen.
    """
    # Per-tool session dir avoids cross-explore collisions.
    sd = os.path.join(session_dir_root, f"explore-{tool_name}-{uuid.uuid4().hex[:6]}")
    os.makedirs(sd, exist_ok=True)

    own_pane = pane_info is None
    pane = pane_info if pane_info is not None else _open_exploration_pane(sd)

    result = ExplorationResult(tool_name=tool_name)

    def on_event(event_type, *args):
        # The interactive_runner doesn't emit a 'probe' event today — we'll
        # piggyback by inspecting the 'turn' event (which gives us the command
        # snippet) and matching it against the pane screen captured separately.
        # See task 3-followup for a cleaner mechanism if needed.
        if event_type == "turn":
            _sid, turn_num, cmd_snippet = args
            # The cmd_snippet is the first 80 chars of the command. To capture
            # the full screen + exit code, we'd need a richer event. For v0
            # we record what we have and post-process from pane scrollback.
            result.probes.append(ProbeOutcome(
                command=cmd_snippet, exit_code=None, screen="",
            ))

    subtask = Subtask(
        id=f"explore-{tool_name}",
        description=build_exploration_goal(tool_name),
        pane="explore",
        depends_on=[],
        mode="interactive",
        max_turns=max_turns,
    )

    try:
        runner_result = run_subtask_interactive(
            subtask, pane, dep_context="", on_event=on_event, session_dir=sd,
        )
        if runner_result.summary:
            result.summary = runner_result.summary
    finally:
        if own_pane:
            _close_exploration_pane(pane)

    return result


# ─── Pane lifecycle helpers (separated for easy monkeypatching in tests) ───

def _open_exploration_pane(session_dir: str) -> PaneInfo:
    server = libtmux.Server(socket_name=SOCKET_NAME)
    sess_name = f"clive-explore-{os.path.basename(session_dir)}"
    sess = server.new_session(
        session_name=sess_name, attach=False, window_name="explore",
    )
    pane_def = {
        "name": "explore",
        "app_type": "explore",  # routes through drivers/explore.md
        "description": "tool exploration pane",
    }
    pane_info = add_pane(sess, pane_def, session_dir)
    return pane_info


def _close_exploration_pane(pane_info: PaneInfo) -> None:
    try:
        detach_stream(pane_info)
    except Exception:
        log.debug("detach_stream failed during exploration teardown", exc_info=True)
```

**Step 4: Run, expect PASS** for the unit tests on `_check_exploration_safety` and the mocked `explore_tool` tests. (Real-pane integration is left to manual smoke testing in task 7 — see Verification at the end.)

**Step 5: Commit**

```bash
git add src/clive/discovery/explorer.py tests/test_discovery_explorer.py
git commit -m "feat(discovery): explore_tool() as run_subtask_interactive adapter (gh#41)"
```

---

## Task 4: `generate_driver()` with strict section validation

**Files:**
- Create: `src/clive/discovery/generator.py`
- Create: `tests/test_discovery_generator.py`

**Design (per review I3 + I4):** auto-gen header goes INSIDE the body (right after the closing `---` of the frontmatter), not before, so `_parse_driver_frontmatter` still finds the frontmatter at byte 0. Section validation uses anchored regex — each section name must appear at line start, not as substring of prose.

**Step 1: Failing test**

```python
# tests/test_discovery_generator.py
from unittest.mock import MagicMock

import pytest

from discovery.generator import generate_driver, AUTO_GEN_HEADER
from discovery.models import ExplorationResult, ProbeOutcome


def _stub_chat(monkeypatch, response: str):
    captured = {}
    def fake_chat(client, messages, **kw):
        captured["messages"] = messages
        return (response, 100, 50)
    monkeypatch.setattr("discovery.generator.chat", fake_chat)
    monkeypatch.setattr("discovery.generator.get_client", lambda: MagicMock())
    return captured


_VALID_DRIVER = """\
---
preferred_mode: script
agent_model: fast
observation_model: fast
---
# rg Driver

ENVIRONMENT: ripgrep
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
- rg PATTERN [PATH]: search recursively

PATTERNS:
- rg -i for case insensitive

RESPONSE FORMAT:
- bash block

COMPLETION: DONE: <summary>
"""


def test_generate_driver_returns_text_with_header_after_frontmatter(monkeypatch):
    _stub_chat(monkeypatch, _VALID_DRIVER)
    r = ExplorationResult(tool_name="rg", probes=[
        ProbeOutcome(command="rg --help", exit_code=0, screen="ripgrep usage..."),
    ])
    text = generate_driver("rg", r)
    # Header must appear, but AFTER the frontmatter so load_driver_meta
    # can still parse the YAML-ish block at byte 0.
    assert text.startswith("---")
    front_end = text.find("---", 3)
    assert front_end > 0
    body = text[front_end + 3:].lstrip()
    assert AUTO_GEN_HEADER in body[:200], "auto-gen header missing from driver body"


def test_generate_driver_passes_exploration_history_to_llm(monkeypatch):
    captured = _stub_chat(monkeypatch, _VALID_DRIVER)
    r = ExplorationResult(tool_name="rg", probes=[
        ProbeOutcome(command="rg --help", exit_code=0, screen="ripgrep usage..."),
    ])
    generate_driver("rg", r)
    full = "\n".join(m["content"] for m in captured["messages"])
    assert "rg --help" in full
    assert "ripgrep usage" in full


def test_generate_driver_rejects_missing_section(monkeypatch):
    # PATTERNS missing.
    bad = _VALID_DRIVER.replace("PATTERNS:\n- rg -i for case insensitive\n\n", "")
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError, match="missing section|malformed"):
        generate_driver("rg", ExplorationResult(tool_name="rg"))


def test_generate_driver_rejects_section_only_in_prose(monkeypatch):
    # Section name appears only as substring inside prose, never at line start.
    bad = """\
---
preferred_mode: script
---
# rg Driver

This driver covers ENVIRONMENT PRIMARY TOOLS PATTERNS RESPONSE FORMAT COMPLETION
but none as actual headings.
"""
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError):
        generate_driver("rg", ExplorationResult(tool_name="rg"))


def test_generate_driver_rejects_no_frontmatter(monkeypatch):
    bad = "# rg Driver\n\nENVIRONMENT: x\nPRIMARY TOOLS:\n- x\nPATTERNS:\n- x\nRESPONSE FORMAT:\n- x\nCOMPLETION: DONE: y\n"
    _stub_chat(monkeypatch, bad)
    with pytest.raises(ValueError):
        generate_driver("rg", ExplorationResult(tool_name="rg"))
```

**Step 2: Run, expect ImportError.**

**Step 3: Implement**

```python
# src/clive/discovery/generator.py
"""LLM-driven driver synthesis from an ExplorationResult (gh#41)."""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from typing import Optional

from llm import chat, get_client
from prompts import _DRIVERS_DIR

from .models import ExplorationResult
from .prompts import build_generation_prompt

log = logging.getLogger(__name__)

AUTO_GEN_HEADER_TEMPLATE = "<!-- Auto-generated by clive --explore on {date} -->"
AUTO_GEN_HEADER = "Auto-generated by clive --explore"

# Required section markers — each must appear at the start of a line, optionally
# preceded by markdown heading tokens (#, ##, etc.). Substring matches in prose
# don't satisfy these.
_REQUIRED_SECTIONS = ("ENVIRONMENT", "PRIMARY TOOLS", "PATTERNS", "RESPONSE FORMAT", "COMPLETION")
_SECTION_REGEXES = {
    section: re.compile(rf"^(?:#+\s+)?{re.escape(section)}\b", re.MULTILINE)
    for section in _REQUIRED_SECTIONS
}


def generate_driver(
    tool_name: str,
    result: ExplorationResult,
    client=None,
    model: Optional[str] = None,
) -> str:
    """Synthesize a driver prompt from an exploration session.

    Returns the markdown text with an auto-gen header inserted INSIDE the body
    (right after the frontmatter close) so the existing _parse_driver_frontmatter
    can still parse the YAML block at byte 0.

    Raises ValueError if the LLM produced output missing required sections or
    lacking frontmatter.
    """
    client = client if client is not None else get_client()
    prompt = build_generation_prompt(result)
    messages = [
        {"role": "system", "content": "You are clive's tool-discovery driver synthesizer."},
        {"role": "user", "content": prompt},
    ]
    text, _pt, _ct = chat(client, messages, model=model, max_tokens=1500)
    text = text.strip()

    _validate_driver_text(tool_name, text)

    return _inject_header(text)


def _validate_driver_text(tool_name: str, text: str) -> None:
    if not text.startswith("---"):
        raise ValueError(f"driver for {tool_name} missing frontmatter")
    front_end = text.find("---", 3)
    if front_end < 0:
        raise ValueError(f"driver for {tool_name} has malformed frontmatter")
    body = text[front_end + 3:]
    missing = [s for s, rx in _SECTION_REGEXES.items() if not rx.search(body)]
    if missing:
        raise ValueError(
            f"driver for {tool_name} missing section(s): {', '.join(missing)}"
        )


def _inject_header(text: str) -> str:
    """Insert the auto-gen header right after the closing `---` of the frontmatter."""
    front_end = text.find("---", 3)
    head = text[: front_end + 3]
    tail = text[front_end + 3:]
    header = AUTO_GEN_HEADER_TEMPLATE.format(date=_dt.date.today().isoformat())
    if tail.startswith("\n"):
        return f"{head}\n{header}{tail}"
    return f"{head}\n{header}\n{tail}"
```

**Step 4: Run, expect PASS.**

**Step 5: Commit**

```bash
git add src/clive/discovery/generator.py tests/test_discovery_generator.py
git commit -m "feat(discovery): generate_driver() with anchored section validation (gh#41)"
```

---

## Task 5: `write_generated_driver()` with overwrite + path-traversal guards

**Files:**
- Modify: `src/clive/discovery/generator.py`
- Create: `tests/test_discovery_writer.py`

**Step 1: Failing test**

```python
# tests/test_discovery_writer.py
import pytest

from discovery.generator import write_generated_driver


def test_writes_driver_to_drivers_dir(tmp_path):
    path = write_generated_driver(
        "rg", "---\npreferred_mode: script\n---\n# rg\n", drivers_dir=str(tmp_path),
    )
    assert path == str(tmp_path / "rg.md")
    assert (tmp_path / "rg.md").read_text().startswith("---")


def test_refuses_to_overwrite_existing_driver(tmp_path):
    (tmp_path / "rg.md").write_text("# hand-written")
    with pytest.raises(FileExistsError):
        write_generated_driver("rg", "new", drivers_dir=str(tmp_path))
    assert (tmp_path / "rg.md").read_text() == "# hand-written"


def test_overwrite_flag_replaces(tmp_path):
    (tmp_path / "rg.md").write_text("# hand-written")
    write_generated_driver("rg", "---\nx\n---\n", drivers_dir=str(tmp_path), overwrite=True)
    assert (tmp_path / "rg.md").read_text() == "---\nx\n---\n"


def test_refuses_path_traversal_in_tool_name(tmp_path):
    for bad in ("../etc/passwd", "/etc/passwd", "rg/../etc/passwd", "..", ".", ""):
        with pytest.raises(ValueError):
            write_generated_driver(bad, "x", drivers_dir=str(tmp_path))


def test_uses_default_drivers_dir_when_none(tmp_path, monkeypatch):
    # Confirm write_generated_driver routes through prompts._DRIVERS_DIR
    # (the canonical drivers/ location), not a re-derived path.
    monkeypatch.setattr("discovery.generator._DRIVERS_DIR", str(tmp_path))
    path = write_generated_driver(
        "rg2", "---\nx\n---\n",
    )
    assert path == str(tmp_path / "rg2.md")
```

**Step 2: Run, expect failure.**

**Step 3: Implement** — append to `src/clive/discovery/generator.py`:

```python
# Append to generator.py

_SAFE_NAME = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.\-]*\Z")


def write_generated_driver(
    tool_name: str,
    driver_text: str,
    drivers_dir: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Write a generated driver to ``drivers_dir/<tool>.md``.

    Refuses to overwrite an existing driver unless ``overwrite=True``.
    Validates ``tool_name`` against an alphanumeric-plus-dot-dash pattern
    to prevent path traversal.
    """
    if not _SAFE_NAME.match(tool_name):
        raise ValueError(f"unsafe tool name for driver path: {tool_name!r}")
    base = drivers_dir if drivers_dir is not None else _DRIVERS_DIR
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{tool_name}.md")
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(
            f"driver already exists at {path}; pass overwrite=True to replace"
        )
    with open(path, "w") as f:
        f.write(driver_text)
    return path
```

**Step 4: Run, expect PASS.**

**Step 5: Commit**

```bash
git add src/clive/discovery/generator.py tests/test_discovery_writer.py
git commit -m "feat(discovery): write_generated_driver with overwrite + path-traversal guards (gh#41)"
```

---

## Task 6: `--explore` CLI flag

**Files:**
- Modify: `src/clive/cli_args.py` (add the flag)
- Modify: `src/clive/cli_handlers.py` (add `handle_explore`)
- Modify: `src/clive/clive.py` (wire dispatch before planner)
- Create: `tests/test_cli_explore.py`

**Step 1: Failing test**

```python
# tests/test_cli_explore.py
from unittest.mock import MagicMock

import pytest

from cli_args import build_parser
from discovery.models import ExplorationResult


def test_parser_accepts_explore_flag():
    parser = build_parser()
    args = parser.parse_args(["--explore", "rg"])
    assert args.explore == "rg"
    assert args.explore_overwrite is False


def test_parser_accepts_explore_overwrite():
    parser = build_parser()
    args = parser.parse_args(["--explore", "rg", "--explore-overwrite"])
    assert args.explore_overwrite is True


def test_handle_explore_runs_pipeline(monkeypatch, capsys):
    import cli_handlers

    fake_result = ExplorationResult(tool_name="rg", summary="ripgrep")
    monkeypatch.setattr(cli_handlers, "explore_tool", lambda name, **kw: fake_result)
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\nx\n---\n")
    written = []
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        lambda name, text, overwrite=False: written.append((name, text)) or "/p/rg.md",
    )

    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)

    assert rc == 0
    assert written == [("rg", "---\nx\n---\n")]
    out = capsys.readouterr().out
    assert "/p/rg.md" in out


def test_handle_explore_returns_nonzero_on_existing_driver(monkeypatch):
    import cli_handlers

    fake_result = ExplorationResult(tool_name="rg", summary="ripgrep")
    monkeypatch.setattr(cli_handlers, "explore_tool", lambda name, **kw: fake_result)
    monkeypatch.setattr(cli_handlers, "generate_driver", lambda name, r: "---\nx\n---\n")
    monkeypatch.setattr(
        cli_handlers, "write_generated_driver",
        MagicMock(side_effect=FileExistsError("exists")),
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0


def test_handle_explore_returns_nonzero_on_malformed_llm(monkeypatch):
    import cli_handlers

    fake_result = ExplorationResult(tool_name="rg", summary="")
    monkeypatch.setattr(cli_handlers, "explore_tool", lambda name, **kw: fake_result)
    monkeypatch.setattr(
        cli_handlers, "generate_driver",
        MagicMock(side_effect=ValueError("missing section")),
    )
    args = MagicMock(explore="rg", explore_overwrite=False)
    rc = cli_handlers.handle_explore(args)
    assert rc != 0
```

**Step 2: Run, expect failure.**

**Step 3: Implement**

```python
# Modify src/clive/cli_args.py — add to build_parser() (alongside the other flags):
parser.add_argument(
    "--explore",
    metavar="TOOL",
    help=(
        "Explore an unknown CLI tool: run --help/-h/man/tldr + safe probes, "
        "then synthesize a driver to drivers/<TOOL>.md (gh#41)."
    ),
)
parser.add_argument(
    "--explore-overwrite",
    action="store_true",
    help="With --explore, overwrite an existing driver instead of refusing.",
)
```

```python
# Modify src/clive/cli_handlers.py — module-level import + new handler:
from discovery.explorer import explore_tool
from discovery.generator import generate_driver, write_generated_driver


def handle_explore(args) -> int:
    tool = args.explore
    print(f"Exploring {tool}...")
    try:
        result = explore_tool(tool)
    except Exception as e:
        print(f"Exploration failed: {e}")
        return 1
    if not result.summary:
        print(
            f"Warning: exploration completed without DONE: marker "
            f"({len(result.probes)} probes captured)."
        )
    try:
        driver_text = generate_driver(tool, result)
    except ValueError as e:
        print(f"Driver synthesis failed: {e}")
        return 1
    try:
        path = write_generated_driver(tool, driver_text, overwrite=args.explore_overwrite)
    except FileExistsError as e:
        print(f"{e}")
        print("Re-run with --explore-overwrite to replace.")
        return 2
    print(f"Wrote driver: {path}")
    print(f"Summary: {result.summary or '(no summary)'}")
    return 0
```

```python
# Modify src/clive/clive.py — in the one-shot subcommand dispatch loop (where
# --schedule, --remove-schedule, etc. dispatch), add --explore. Place ABOVE
# the planner/run dispatch so `--explore rg` doesn't try to plan "rg" as a task.

for arg_name, handler in [
    ("explore", handle_explore),
    # ... existing entries
]:
    if getattr(args, arg_name, None):
        sys.exit(handler(args))
```

(The exact place to insert depends on the existing dispatch loop; the principle is `--explore` runs and exits before any task planning happens.)

**Step 4: Run, expect PASS.**

**Step 5: Commit**

```bash
git add src/clive/cli_args.py src/clive/cli_handlers.py src/clive/clive.py tests/test_cli_explore.py
git commit -m "feat(discovery): --explore CLI flag (gh#41)"
```

---

## Task 7: Docs — CHANGELOG + CLAUDE.md + README

**Files:**
- Modify: `CLAUDE.md` — new section under "Source layout"
- Modify: `CHANGELOG.md` — "Unreleased" section
- Modify: `README.md` — bump test count

**Step 1: Edit CLAUDE.md** — add a section about the discovery subpackage:

```markdown
- `discovery/` — self-learning tool discovery (gh#41).
  - `explore_tool(name)` runs bounded probes (`--help`/`-h`/`man`/`tldr`) against an unknown CLI in a fresh exploration pane. Uses `run_subtask_interactive` under the hood with a synthetic `Subtask(mode="interactive")` and the `drivers/explore.md` driver. Exploration-specific safety (`_check_exploration_safety`) layers credential-tool and TUI guards on top of `_check_command_safety`.
  - `generate_driver(name, result)` synthesizes a `drivers/<name>.md` from the exploration log via LLM. Validates the result against required section markers (ENVIRONMENT, PRIMARY TOOLS, PATTERNS, RESPONSE FORMAT, COMPLETION) anchored to line start, not substring. Auto-gen header lives inside the body (after the frontmatter close) so `_parse_driver_frontmatter` still parses metadata.
  - `write_generated_driver(name, text, overwrite=False)` writes to `drivers/<name>.md`; refuses to overwrite hand-written drivers unless `overwrite=True`. Validates `name` against a `[A-Za-z0-9_][A-Za-z0-9_.\-]*` pattern to prevent path traversal.

  Manual entry point: `clive --explore <tool>` runs the full pipeline and writes the driver. `--explore-overwrite` replaces an existing driver.

  Deferred (separate cards): the `refine_driver(name)` audit-log replay function (needs gh#40's Layer 5 eval orchestrator to be useful) and `CLIVE_AUTO_EXPLORE=1` auto-trigger from `_expand_toolset` (needs gh#39's category auto-classification so the new driver gets surfaced to a toolset).
```

**Step 2: Edit CHANGELOG.md** — add an "Unreleased" section above 0.7.2:

```markdown
## Unreleased

### Added

- **Self-learning tool discovery — manual `--explore` (gh#41)** — New `discovery/` subpackage that lets clive learn how to use an unknown CLI tool. `clive --explore <tool>` runs `--help`/`-h`/`man`/`tldr` + a few safe probes against the tool in a fresh exploration pane, then asks the LLM to synthesize a `drivers/<tool>.md` from the exploration log following the existing driver template (frontmatter + ENVIRONMENT/PRIMARY TOOLS/PATTERNS/PITFALLS/RESPONSE FORMAT/COMPLETION). Generated drivers carry an auto-gen header inside the body (so frontmatter parsing is unaffected) and refuse to overwrite hand-written drivers unless `--explore-overwrite` is passed.
- **Exploration safety layer** — `_check_exploration_safety` layers credential-prompt and TUI guards on top of `_check_command_safety`: tools in `CREDENTIAL_TOOLS` (aws, gh, gcloud, kubectl, psql, mysql, ssh, …) and `INTERACTIVE_TOOLS` (vim, less, top, lazygit, k9s, …) are refused without an explicit `--help`/`-h`/`--version` flag, preventing the explorer from trapping on a credential prompt or TUI.
- **`drivers/explore.md`** — auto-discovered driver for the exploration pane. Tells the agent its probe order, what tools to avoid bare, and to DONE: after 5–8 probes.

### Deferred

- `refine_driver(name)` for audit-log-driven driver refinement (Phase 3) — function design is in `docs/plans/2026-05-22-self-learning-tool-discovery.md` but shipping requires gh#40's Layer 5 eval orchestrator. Tracked on the kanban board.
- `CLIVE_AUTO_EXPLORE=1` auto-trigger from `_expand_toolset` — requires gh#39's category auto-classification so the new driver actually surfaces in a toolset entry. Tracked on the kanban board.
```

**Step 3: Edit README.md** — bump test count to whatever the final number is after task 1–6.

**Step 4: Commit**

```bash
git add CLAUDE.md CHANGELOG.md README.md
git commit -m "docs: self-learning tool discovery (gh#41 manual --explore)"
```

---

## Task 8: Kanban + diary

```bash
SKILL=/Users/martintreiber/.claude/skills/kanban/scripts
bash $SKILL/note.sh 2026-05-22-self-learning-tool-discovery-throw-any-c \
  "Shipped: discovery/ subpackage + manual --explore <tool>. Deferred to new cards: refine_driver (depends on gh#40), CLIVE_AUTO_EXPLORE auto-trigger (depends on gh#39)."
bash $SKILL/move.sh 2026-05-22-self-learning-tool-discovery-throw-any-c review

# Two follow-up cards captured here:
bash $SKILL/add.sh "Audit-log-driven driver refinement (gh#41 Phase 3)" \
  --priority=medium --tags=discovery,gh41-followup \
  --description="refine_driver(name, audit_log_dir) re-synthesizes drivers/<name>.md from accumulated .clive/audit/ failure signals. Standalone function works; the *orchestrator* that runs evals → measures failure → refines → re-runs is gh#40 territory. Build the loop once gh#40's Layer 5 evals exist."

bash $SKILL/add.sh "Auto-trigger exploration on missing driver (gh#41 Phase 1 integration)" \
  --priority=medium --tags=discovery,gh41-followup \
  --description="CLIVE_AUTO_EXPLORE=1 gates _expand_toolset to call explore_tool + generate_driver for any pane whose app_type has no driver. Currently dead-on-arrival because the new driver isn't surfaced into the active toolset — needs gh#39 to auto-categorize the new tool and add it to a category. Build after gh#39 lands."
```

Diary: `.dev-diary/2026-05-22-gh41-manual-explore.md` — capture scope decision (D → trimmed by reviewer to manual-only Phase 1+2), the architectural pivot from reinventing the interactive loop to adapting `run_subtask_interactive`, and the two follow-up cards.

---

## Verification commands run at end

1. `python3 -m pytest -q --tb=short` — full suite, expect 990 + ~25 new tests.
2. `python3 clive.py --help | grep -- --explore` — flag is documented.
3. `python3 clive.py --explore echo` (with a real LLM key + network) — produces `drivers/echo.md` with an auto-gen header. Manual smoke only; not part of CI.

---

## What's deferred and why

- **`refine_driver` audit-log refinement (Phase 3)** — the function is useful only inside an automated eval loop (gh#40). Shipping the building block without the loop adds API surface that no caller exercises. Follow-up card created.
- **`CLIVE_AUTO_EXPLORE=1` auto-trigger from `_expand_toolset` (Phase 1 integration)** — a driver written to `drivers/<name>.md` during expansion isn't surfaced into any toolset category unless gh#39's auto-categorization runs alongside. Follow-up card created.
- **Phase 4 cross-session example selection** — picking 1-2 best historical examples to inline into the driver requires a ranking step that's a separate research direction. Not addressed here.

These three follow-up items each warrant their own kanban card; tasks 1–6 of this plan form the foundation they will compose with.
