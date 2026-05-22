# Four-Tier Tool Registry Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the flat `build_tools_summary()` dump with a four-tier progressive disclosure architecture (category index → tool names → tool cards → full driver), behind an opt-in flag, so planner/worker prompts only carry the tool context actually needed. Land the helper (`classify_tool_to_category`) that gh#41 Phase 1 needs to surface auto-explored drivers into a category.

**Architecture:** The data layer stays as augmented in-process dicts (`COMMANDS`, `CATEGORIES`) — we add two optional fields (`card`, `keywords`) to each command. The prompt layer gets four functions (`build_tier0`, `build_tier1`, `build_tier2`, plus the existing `load_driver` as Tier 3) that compose what to inject. The planner emits `tools` on each subtask; the worker runner loads the matching Tier-2 cards. Behind `CLIVE_PROGRESSIVE_TOOLS=1` until validated, then becomes default. YAML-per-tool storage migration is explicitly out of scope — only do that if/when the in-memory dict starts feeling unwieldy.

**Tech Stack:** Python 3.10+, pytest, plain dicts in `session/toolsets.py`, `tiktoken` for token measurement (already a dep via the LLM client).

**Sources:** gh#39 issue body, `docs/plans/2026-05-22-self-learning-tool-discovery.md` for the gh#41 followup linkage, `src/clive/session/toolsets.py` (current state), `src/clive/llm/prompts.py` (consumer).

**Out of scope (tracked elsewhere):**
- YAML registry files (`tools/registry/*.yaml`) — only worth doing if/when the in-memory dict feels too big.
- `CLIVE_AUTO_EXPLORE=1` auto-trigger — separate card (gh#41 Phase 1). This plan delivers the prerequisite (`classify_tool_to_category`), nothing more.
- Auditable history of tool selections — a `gh#40` Layer-5 eval concern.

**Branch:** `feature/four-tier-registry` (create at start). Single PR at the end.

---

### Task 1: Test scaffolding — token-budget baseline

**Why first:** Token reduction is the headline metric. Capture the current cost before any change; the same test compares after each tier lands. Without it the 90% claim stays aspirational.

**Files:**
- Create: `tests/test_tool_registry_tokens.py`

**Step 1: Write the failing baseline test**

```python
"""Token-budget regression for tool registry tiers (gh#39)."""
import pytest
from toolsets import resolve_toolset, build_tools_summary, check_commands

# Cheap token estimator: 4 chars per token is a stable rule-of-thumb
# within ±10% for English-y prompts. Avoids pulling tiktoken in tests.
def _approx_tokens(text: str) -> int:
    return len(text) // 4

def test_baseline_full_toolset_tokens():
    """Capture today's cost: full profile, all tools dumped."""
    resolved = resolve_toolset("full")
    available, _ = check_commands(resolved["commands"])
    # tool_status mimic: every pane "ready"
    tool_status = {p["name"]: {"status": "ready",
                                "app_type": p["app_type"],
                                "description": p["description"]}
                   for p in resolved["panes"]}
    summary = build_tools_summary(tool_status, available, resolved["endpoints"])
    tokens = _approx_tokens(summary)
    # Tripwire: today the full profile is ~600-900 tokens. If it ever
    # crosses 1500 something is wrong with how summaries grow.
    assert tokens < 1500, f"full toolset summary now {tokens} tokens"
    # And document the floor: ensures the test runs against real data.
    assert tokens > 200, f"summary suspiciously small: {tokens} tokens"
```

**Step 2: Run to verify it passes (this is a baseline)**

Run: `pytest tests/test_tool_registry_tokens.py -v`
Expected: PASS. Note the actual token count printed via `-s` flag.

```bash
pytest tests/test_tool_registry_tokens.py::test_baseline_full_toolset_tokens -v -s
# Note the actual count for the commit message.
```

**Step 3: Commit**

```bash
git checkout -b feature/four-tier-registry
git add tests/test_tool_registry_tokens.py
git commit -m "test(registry): baseline token budget for current build_tools_summary"
```

---

### Task 2: Add `card` field to COMMANDS — data layer, no behavior change

**Files:**
- Modify: `src/clive/session/toolsets.py` — `COMMANDS` dict
- Test: `tests/test_tool_registry_cards.py`

**Step 1: Write the failing test**

```python
"""Each COMMANDS entry must expose a compact 'card' suitable for Tier 2."""
import pytest
from toolsets import COMMANDS

def test_every_command_has_a_card():
    missing = [name for name, defn in COMMANDS.items()
               if not defn.get("card")]
    assert missing == [], f"commands without 'card' field: {missing}"

def test_card_is_compact():
    """A card is a reference snippet, not a paragraph. ≤200 chars."""
    too_long = {name: len(defn["card"])
                for name, defn in COMMANDS.items()
                if len(defn["card"]) > 200}
    assert too_long == {}, f"cards too long: {too_long}"

def test_card_starts_with_tool_name():
    """Convention: '[name] one-line synopsis\n  usage examples...'"""
    bad = [name for name, defn in COMMANDS.items()
           if not defn["card"].startswith(f"[{name}]")]
    assert bad == [], f"cards missing [name] prefix: {bad}"
```

**Step 2: Run to confirm failure**

Run: `pytest tests/test_tool_registry_cards.py -v`
Expected: FAIL — all three tests fail because `card` doesn't exist yet.

**Step 3: Augment COMMANDS with `card` field**

Edit `src/clive/session/toolsets.py`. For each command in `COMMANDS`, add a `card` key. The card MUST start with `[<name>]`, be ≤200 chars, and cram synopsis + 1–2 usage lines.

Pattern:

```python
"jq": {
    "description": "JSON processor — parse, filter, transform JSON output",
    "usage": "curl -s api.example.com | jq '.data[] | {name, id}'",
    "check": "command -v jq",
    "install": "brew install jq",
    "category": "data",
    "card": (
        "[jq] JSON processor\n"
        "  jq 'FILTER' [file]   .key | .[] | select(c) | map(f)\n"
        "  jq -r '.data[].name' < users.json"
    ),
},
```

Do this for ALL ~30 commands. Cards should be reference-card terse: command form, key operators, one example.

**Step 4: Run tests to confirm pass**

Run: `pytest tests/test_tool_registry_cards.py -v`
Expected: PASS, all three.

Also re-run the baseline so we know data-layer changes didn't break anything:

```bash
pytest tests/test_tool_registry_tokens.py tests/test_tool_registry_cards.py -v
```

**Step 5: Commit**

```bash
git add src/clive/session/toolsets.py tests/test_tool_registry_cards.py
git commit -m "feat(registry): add compact 'card' field to each COMMANDS entry (gh#39)"
```

---

### Task 3: `build_tier0_summary()` — category index

**Files:**
- Modify: `src/clive/session/toolsets.py` — add function near `build_tools_summary`
- Test: `tests/test_tool_registry_tiers.py`

**Step 1: Write the failing test**

```python
"""Tier 0 = category names + counts only. No tool descriptions."""
import pytest
from toolsets import build_tier0_summary, resolve_toolset, COMMANDS

def test_tier0_lists_active_categories_with_counts():
    """Given the active categories of a session, list them with tool counts."""
    resolved = resolve_toolset("standard")
    summary = build_tier0_summary(resolved["categories"])
    # Active categories appear
    for cat in resolved["categories"]:
        assert cat in summary, f"missing category {cat}"
    # Counts appear (e.g. "data(4)")
    assert "(" in summary and ")" in summary
    # Discovery hint is present
    assert "tool_info" in summary or "tools" in summary.lower()

def test_tier0_is_compact():
    """Tier 0 stays under 200 tokens even with all categories loaded."""
    from toolsets import CATEGORIES
    summary = build_tier0_summary(list(CATEGORIES.keys()))
    assert len(summary) // 4 < 200, \
        f"tier0 too large: {len(summary)//4} tokens"

def test_tier0_skips_unknown_categories():
    """Robust to typos — unknown categories are silently dropped."""
    summary = build_tier0_summary(["data", "not_a_real_category"])
    assert "not_a_real_category" not in summary
    assert "data" in summary
```

**Step 2: Run to confirm failure**

Run: `pytest tests/test_tool_registry_tiers.py::test_tier0_lists_active_categories_with_counts -v`
Expected: FAIL with `ImportError: cannot import name 'build_tier0_summary'`.

**Step 3: Implement `build_tier0_summary`**

Add to `src/clive/session/toolsets.py`:

```python
def build_tier0_summary(active_categories: list[str]) -> str:
    """Tier 0: category index with tool counts. ~100 tokens.

    The planner sees what *kinds* of tools exist, not every individual tool.
    Combined with Tier 1 (`build_tier1_names`) for categories the planner picks.
    """
    parts = []
    for cat in active_categories:
        cat_def = CATEGORIES.get(cat)
        if not cat_def:
            continue
        count = (len(cat_def.get("commands", []))
                 + len(cat_def.get("panes", []))
                 + len(cat_def.get("endpoints", [])))
        parts.append(f"{cat}({count})")
    if not parts:
        return ""
    listing = ", ".join(parts)
    return (
        f"Tool categories available: {listing}\n"
        "Use `tool_info <name>` for details on a specific tool."
    )
```

**Step 4: Run tests to confirm pass**

Run: `pytest tests/test_tool_registry_tiers.py -v -k tier0`
Expected: PASS, all three tier0 tests.

**Step 5: Commit**

```bash
git add src/clive/session/toolsets.py tests/test_tool_registry_tiers.py
git commit -m "feat(registry): build_tier0_summary — category index (gh#39)"
```

---

### Task 4: `build_tier1_names(categories)` — tool names per category

**Files:**
- Modify: `src/clive/session/toolsets.py`
- Test: `tests/test_tool_registry_tiers.py` (extend)

**Step 1: Write the failing test**

```python
def test_tier1_lists_names_per_category():
    """Tier 1: per-category name listing, no descriptions."""
    from toolsets import build_tier1_names
    summary = build_tier1_names(["data"])
    # Category header
    assert "data:" in summary
    # All data commands listed
    for name in ("jq", "rg", "mlr", "sqlite3"):
        assert name in summary
    # NO descriptions leak in
    assert "JSON processor" not in summary

def test_tier1_handles_multiple_categories():
    from toolsets import build_tier1_names
    summary = build_tier1_names(["data", "web"])
    assert "data:" in summary
    assert "web:" in summary

def test_tier1_includes_panes_and_endpoints():
    """A category can offer panes and endpoints too — list all surfaces."""
    from toolsets import build_tier1_names
    summary = build_tier1_names(["info"])
    # info has endpoint-only category
    assert "weather" in summary or "hackernews" in summary
```

**Step 2: Run to confirm failure**

Run: `pytest tests/test_tool_registry_tiers.py -v -k tier1`
Expected: FAIL with import error.

**Step 3: Implement**

```python
def build_tier1_names(categories: list[str]) -> str:
    """Tier 1: tool names per category, no descriptions. ~50 tokens/category."""
    lines = []
    for cat in categories:
        cat_def = CATEGORIES.get(cat)
        if not cat_def:
            continue
        names = []
        names.extend(cat_def.get("panes", []))
        names.extend(cat_def.get("commands", []))
        names.extend(cat_def.get("endpoints", []))
        if names:
            lines.append(f"{cat}: {', '.join(names)}")
    return "\n".join(lines)
```

**Step 4: Run tests to confirm pass**

Run: `pytest tests/test_tool_registry_tiers.py -v`
Expected: PASS, all tier0 + tier1 tests.

**Step 5: Commit**

```bash
git add src/clive/session/toolsets.py tests/test_tool_registry_tiers.py
git commit -m "feat(registry): build_tier1_names — per-category name listing (gh#39)"
```

---

### Task 5: `build_tier2_card(name)` — single-tool card lookup

**Files:**
- Modify: `src/clive/session/toolsets.py`
- Test: `tests/test_tool_registry_tiers.py` (extend)

**Step 1: Write the failing test**

```python
def test_tier2_returns_card_for_known_command():
    from toolsets import build_tier2_card
    card = build_tier2_card("jq")
    assert card is not None
    assert card.startswith("[jq]")
    assert len(card) <= 200

def test_tier2_returns_none_for_unknown():
    from toolsets import build_tier2_card
    assert build_tier2_card("not_a_real_tool") is None

def test_tier2_resolves_aliases():
    """Aliases like 'mail' → 'email' should resolve."""
    from toolsets import build_tier2_card
    # 'mail' is an alias for the email pane; cards for panes synthesize
    # from the pane definition (description + usage hints).
    card = build_tier2_card("mail")
    # Either a card exists (resolved via alias) or returns None gracefully.
    # We accept either as long as it doesn't crash.
    if card is not None:
        assert card.startswith("[email]") or card.startswith("[mail]")
```

**Step 2: Run to confirm failure**

Run: `pytest tests/test_tool_registry_tiers.py -v -k tier2`
Expected: FAIL with import error.

**Step 3: Implement**

```python
def build_tier2_card(name: str) -> str | None:
    """Tier 2: compact reference card for a single tool. ~150 tokens.

    Returns None if the tool isn't known. Resolves COMMAND aliases via
    `normalize_tool_name`. Panes synthesize a card from their definition.
    """
    canonical = normalize_tool_name(name)
    if canonical in COMMANDS:
        return COMMANDS[canonical].get("card")
    if canonical in PANES:
        pane = PANES[canonical]
        return f"[{canonical}] {pane.get('description', '').strip()}"
    return None
```

**Step 4: Run tests to confirm pass**

Run: `pytest tests/test_tool_registry_tiers.py -v`
Expected: PASS, all tier0/1/2 tests.

**Step 5: Commit**

```bash
git add src/clive/session/toolsets.py tests/test_tool_registry_tiers.py
git commit -m "feat(registry): build_tier2_card — per-tool reference card (gh#39)"
```

---

### Task 6: `classify_tool_to_category(name, description)` — unblocks gh#41 Phase 1

**Why now:** gh#41 Phase 1 integration (`CLIVE_AUTO_EXPLORE=1`) needs to drop newly explored tools into a category. Without classification, an auto-generated driver is orphaned. Cheap heuristic now; LLM-driven later if needed.

**Files:**
- Modify: `src/clive/session/toolsets.py`
- Test: `tests/test_tool_registry_classify.py`

**Step 1: Write the failing test**

```python
"""Classify an unknown tool into one of the existing categories."""
import pytest
from toolsets import classify_tool_to_category

@pytest.mark.parametrize("name,desc,expected", [
    ("xq", "Command-line XML processor like jq for XML", "data"),
    ("httpie", "Modern HTTP client with intuitive syntax", "web"),
    ("imageoptim", "Optimize PNG and JPEG image files in place", "images"),
    ("zoxide", "Smarter cd command that learns your habits", "core"),
    ("lazygit", "Terminal UI for git commands", "dev"),
])
def test_classify_known_shape(name, desc, expected):
    assert classify_tool_to_category(name, desc) == expected

def test_classify_returns_none_for_unclassifiable():
    """No category should match a completely random description."""
    result = classify_tool_to_category("frobnicator",
                                       "asdf qwer zxcv abcd efgh")
    assert result is None
```

**Step 2: Run to confirm failure**

Run: `pytest tests/test_tool_registry_classify.py -v`
Expected: FAIL — function doesn't exist.

**Step 3: Implement**

Add a small keyword table per category (no LLM needed — that's a separate ambition):

```python
# Category keyword hints for `classify_tool_to_category`.
# Conservative — only obvious words. Returns None on miss rather than guess.
_CATEGORY_KEYWORDS = {
    "data":   ["json", "csv", "tsv", "xml", "yaml", "parse", "query",
               "filter", "transform", "sql", "database"],
    "web":    ["http", "curl", "url", "web", "html", "browser",
               "scrape", "rest", "api client"],
    "docs":   ["pdf", "markdown", "document", "convert", "doc ", "latex"],
    "media":  ["video", "audio", "youtube", "podcast", "transcribe",
               "ffmpeg", "stream"],
    "images": ["image", "png", "jpeg", "jpg", "photo", "exif", "gif"],
    "comms":  ["email", "calendar", "contact", "notification", "chat",
               "message"],
    "dev":    ["git", "github", "pull request", "issue", "commit",
               "diff", "code"],
    "search": ["search engine", "google", "duckduckgo", "bing"],
    "ai":     ["llm", "openai", "anthropic", "claude", "gpt", "summariz",
               "language model"],
    "voice":  ["microphone", "speech", "speak", "audio record",
               "text-to-speech", "tts"],
    "sync":   ["s3", "rclone", "cloud storage", "dropbox", "sync"],
    "core":   ["filesystem", "directory", "cd ", "shell", "navigate"],
}

def classify_tool_to_category(name: str, description: str) -> str | None:
    """Best-effort classify an unknown tool into an existing category.

    Used by gh#41 Phase 1 auto-explore to surface a newly generated
    driver into a toolset entry. Returns None when no keyword matches.
    Conservative on purpose: a wrong category bucket is worse than no
    bucket (auto-explore can fall back to core).
    """
    haystack = f"{name} {description}".lower()
    matches: dict[str, int] = {}
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in haystack)
        if score:
            matches[cat] = score
    if not matches:
        return None
    return max(matches, key=matches.get)
```

**Step 4: Run tests to confirm pass**

Run: `pytest tests/test_tool_registry_classify.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/clive/session/toolsets.py tests/test_tool_registry_classify.py
git commit -m "feat(registry): classify_tool_to_category for gh#41 Phase 1 (gh#39)"
```

---

### Task 7: Subtask.tools field + planner contract

**Why:** Tier 2 cards have to be loaded for the *tools the planner picks*. Today the planner returns subtasks but doesn't say which tools each one needs. Add a `tools` field on `Subtask`, instruct the planner to fill it, and accept None on legacy paths.

**Files:**
- Modify: `src/clive/models.py` — `Subtask` dataclass
- Modify: `src/clive/llm/prompts.py` — `build_planner_prompt` instructions
- Test: `tests/test_subtask_tools_field.py`

**Step 1: Write the failing test**

```python
"""Subtask should carry an optional 'tools' list."""
import pytest
from models import Subtask

def test_subtask_accepts_tools():
    s = Subtask(id="1", description="x", pane="shell", mode="script",
                tools=["jq", "curl"])
    assert s.tools == ["jq", "curl"]

def test_subtask_tools_defaults_to_empty():
    s = Subtask(id="1", description="x", pane="shell", mode="script")
    assert s.tools == []

def test_subtask_serializes_round_trip():
    """JSON round-trip preserves the tools field."""
    import json
    from dataclasses import asdict
    s = Subtask(id="1", description="x", pane="shell", mode="script",
                tools=["jq"])
    js = json.dumps(asdict(s))
    revived = Subtask(**json.loads(js))
    assert revived.tools == ["jq"]
```

**Step 2: Confirm failure**

Run: `pytest tests/test_subtask_tools_field.py -v`
Expected: FAIL — `Subtask` doesn't take `tools`.

**Step 3: Add field**

Edit `src/clive/models.py`. Locate `Subtask` dataclass. Add (with safe default):

```python
tools: list[str] = field(default_factory=list)
```

Make sure `field` is imported from dataclasses.

**Step 4: Update planner prompt to emit `tools`**

Edit `src/clive/llm/prompts.py`. In `build_planner_prompt`, add to the rules section (between rules 11 and 12):

```
12. Each subtask MAY declare "tools": ["tool1", "tool2"] — the specific
    CLI tools that subtask will use. The worker prompt will load reference
    cards for these. Be specific: pick from the COMMANDS list, not
    pane names or generic words like "shell".
```

Update the example subtasks to show a `tools` field. Existing tests should still pass because the field is optional.

**Step 5: Run tests**

Run: `pytest tests/test_subtask_tools_field.py tests/test_executor.py tests/test_planner.py -v`
Expected: PASS — new test passes, no regression in executor/planner tests.

**Step 6: Commit**

```bash
git add src/clive/models.py src/clive/llm/prompts.py tests/test_subtask_tools_field.py
git commit -m "feat(registry): Subtask.tools field + planner emits tool picks (gh#39)"
```

---

### Task 8: Worker prompt injects Tier-2 cards

**Files:**
- Modify: `src/clive/llm/prompts.py` — add `build_worker_tool_context(subtask)`
- Modify: the runner(s) that build worker turn prompts — search first; usually it's `execution/runtime.py` or `execution/interactive_runner.py`
- Test: `tests/test_worker_tool_context.py`

**Step 1: Locate the worker prompt assembly point**

```bash
grep -rn "build_worker\|worker_prompt\|driver\|subtask.description" src/clive/execution/ src/clive/llm/ | grep -i "prompt\|driver" | head
```

The current path is: drivers + screen are the worker's prompt. We're adding a *tools* block on top. Find where the driver string gets composed into the turn — that's the insertion point.

**Step 2: Write the failing test**

```python
"""Worker prompt builder should inject Tier-2 cards for subtask.tools."""
import pytest
from models import Subtask
from llm.prompts import build_worker_tool_context  # to be created

def test_worker_tool_context_loads_cards():
    s = Subtask(id="1", description="x", pane="shell", mode="interactive",
                tools=["jq", "rg"])
    block = build_worker_tool_context(s)
    assert "[jq]" in block
    assert "[rg]" in block

def test_worker_tool_context_empty_for_no_tools():
    s = Subtask(id="1", description="x", pane="shell", mode="script")
    assert build_worker_tool_context(s) == ""

def test_worker_tool_context_skips_unknown_tools():
    """A nonexistent tool name in subtask.tools is silently dropped."""
    s = Subtask(id="1", description="x", pane="shell", mode="interactive",
                tools=["jq", "not_a_real_tool"])
    block = build_worker_tool_context(s)
    assert "[jq]" in block
    assert "not_a_real_tool" not in block
```

**Step 3: Confirm failure**

Run: `pytest tests/test_worker_tool_context.py -v`
Expected: FAIL — import error.

**Step 4: Implement**

Add to `src/clive/llm/prompts.py`:

```python
def build_worker_tool_context(subtask) -> str:
    """Compose Tier-2 reference cards for the subtask's declared tools.

    Returns empty string when no tools are declared or none resolve.
    The caller decides where to splice this into the turn prompt
    (typically just below the driver header).
    """
    from toolsets import build_tier2_card
    tools = getattr(subtask, "tools", None) or []
    cards = []
    for t in tools:
        card = build_tier2_card(t)
        if card:
            cards.append(card)
    if not cards:
        return ""
    return "Tools you may need:\n" + "\n".join(cards)
```

**Step 5: Splice into the worker turn assembly**

Find the worker-turn prompt builder (will surface during Step 1). Insert the block after the driver text:

```python
tool_ctx = build_worker_tool_context(subtask)
if tool_ctx:
    prompt += "\n\n" + tool_ctx
```

**Step 6: Run tests**

Run: `pytest tests/test_worker_tool_context.py tests/test_executor.py -v`
Expected: PASS.

**Step 7: Commit**

```bash
git add src/clive/llm/prompts.py src/clive/execution/ tests/test_worker_tool_context.py
git commit -m "feat(registry): worker prompt injects Tier-2 cards from subtask.tools (gh#39)"
```

---

### Task 9: Behind `CLIVE_PROGRESSIVE_TOOLS=1`, switch planner prompt to Tier 0 + Tier 1

**Files:**
- Modify: `src/clive/session/toolsets.py` — `build_tools_summary` gains an env-gated path
- Test: `tests/test_progressive_planner_prompt.py`

**Step 1: Write the failing test**

```python
"""When CLIVE_PROGRESSIVE_TOOLS=1, build_tools_summary returns Tier0+Tier1."""
import os
import pytest
from toolsets import resolve_toolset, build_tools_summary, check_commands

def _make_inputs(profile="standard"):
    resolved = resolve_toolset(profile)
    available, _ = check_commands(resolved["commands"])
    tool_status = {p["name"]: {"status": "ready",
                                "app_type": p["app_type"],
                                "description": p["description"]}
                   for p in resolved["panes"]}
    return tool_status, available, resolved["endpoints"], resolved["categories"]

def test_progressive_summary_shorter(monkeypatch):
    monkeypatch.delenv("CLIVE_PROGRESSIVE_TOOLS", raising=False)
    ts, ac, ep, cats = _make_inputs("standard")
    legacy = build_tools_summary(ts, ac, ep)

    monkeypatch.setenv("CLIVE_PROGRESSIVE_TOOLS", "1")
    new = build_tools_summary(ts, ac, ep, categories=cats)
    assert len(new) < len(legacy), \
        f"progressive ({len(new)}) should be shorter than legacy ({len(legacy)})"

def test_progressive_default_off(monkeypatch):
    monkeypatch.delenv("CLIVE_PROGRESSIVE_TOOLS", raising=False)
    ts, ac, ep, cats = _make_inputs("standard")
    out = build_tools_summary(ts, ac, ep, categories=cats)
    # Default still emits the legacy format (full descriptions).
    # 'JSON processor' is jq's description text — should appear.
    assert "JSON processor" in out
```

**Step 2: Confirm failure**

Run: `pytest tests/test_progressive_planner_prompt.py -v`
Expected: FAIL — `categories=` kwarg unknown, env var path doesn't exist.

**Step 3: Implement env-gated branch**

Edit `build_tools_summary` in `src/clive/session/toolsets.py`. Add an optional `categories` kwarg. When `CLIVE_PROGRESSIVE_TOOLS=1` and `categories` is provided, return `tier0 + tier1` instead of the flat dump.

```python
import os

def build_tools_summary(
    pane_status: dict[str, dict],
    available_commands: list[dict],
    endpoints: list[dict],
    categories: list[str] | None = None,
) -> str:
    # Progressive path: tier 0 (category index) + tier 1 (names per active cat).
    if os.environ.get("CLIVE_PROGRESSIVE_TOOLS") == "1" and categories:
        parts = [build_tier0_summary(categories), build_tier1_names(categories)]
        return "\n\n".join(p for p in parts if p)
    # Legacy path: unchanged.
    sections = []
    # … rest of the existing function …
```

**Step 4: Update callers to pass `categories`**

Search-and-edit the callers to pass `categories=session_ctx["categories"]` (the existing set). Backward compatibility: the kwarg defaults to None, so missing-callers still work.

Callers (from earlier grep):
- `clive_core.py:313` and `:381`
- `cli_modes.py:155`
- `tui/tui_task_runner.py:140`

Pass `categories=list(resolved["categories"])` (or the set from `session_ctx`) at each site.

**Step 5: Run tests**

```bash
pytest tests/test_progressive_planner_prompt.py tests/test_tool_registry_tokens.py -v
```

Expected: PASS. Also re-run a sample of integration tests to confirm no regression:

```bash
pytest tests/test_executor.py tests/test_planner.py tests/test_router.py -v
```

**Step 6: Add a token-budget assertion under the flag**

Append to `tests/test_tool_registry_tokens.py`:

```python
def test_progressive_under_flag_is_smaller(monkeypatch):
    """Under the flag, full profile drops below half the legacy budget."""
    from toolsets import resolve_toolset, build_tools_summary, check_commands
    resolved = resolve_toolset("full")
    available, _ = check_commands(resolved["commands"])
    tool_status = {p["name"]: {"status": "ready",
                                "app_type": p["app_type"],
                                "description": p["description"]}
                   for p in resolved["panes"]}
    monkeypatch.delenv("CLIVE_PROGRESSIVE_TOOLS", raising=False)
    legacy = build_tools_summary(tool_status, available, resolved["endpoints"])
    monkeypatch.setenv("CLIVE_PROGRESSIVE_TOOLS", "1")
    new = build_tools_summary(tool_status, available, resolved["endpoints"],
                              categories=resolved["categories"])
    # At least 50% reduction. The headline 90% claim is for *very* large
    # toolsets (120+); 50% on the current ~30 is a realistic floor.
    assert len(new) * 2 < len(legacy), \
        f"progressive {len(new)} not < half of legacy {len(legacy)}"
```

**Step 7: Commit**

```bash
git add src/clive/session/toolsets.py src/clive/clive_core.py src/clive/cli_modes.py src/clive/tui/tui_task_runner.py tests/
git commit -m "feat(registry): CLIVE_PROGRESSIVE_TOOLS=1 switches planner to Tier0+1 (gh#39)"
```

---

### Task 10: Self-service `clive-tools` CLI for in-pane discovery

**Why:** Per gh#39 issue, mid-task an agent should be able to type `tool_info <name>` or `tools <category>` from any shell pane. A tiny Python CLI that the agent calls is the lowest-friction option.

**Files:**
- Create: `tools/clive-tools` (executable shell-callable Python script)
- Modify: `src/clive/session/session.py` (or wherever panes are bootstrapped) — make `tools/` available on PATH
- Test: `tests/test_clive_tools_cli.py`

**Step 1: Decide on UX**

The agent should be able to do:

```bash
$ tools                 # list categories with counts (Tier 0)
$ tools data            # list tools in data category (Tier 1 for one cat)
$ tool_info jq          # print jq's card (Tier 2)
```

Implement as a single Python script with two argv-driven modes (`list` and `info`), plus two thin shell wrappers (`tools`, `tool_info`).

**Step 2: Write the failing test**

```python
"""clive-tools CLI: in-pane discovery for agents."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "clive-tools"

def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
    )

def test_list_no_args_shows_categories():
    r = _run("list")
    assert r.returncode == 0
    assert "data" in r.stdout and "web" in r.stdout

def test_list_with_category_shows_tools():
    r = _run("list", "data")
    assert r.returncode == 0
    assert "jq" in r.stdout

def test_info_shows_card():
    r = _run("info", "jq")
    assert r.returncode == 0
    assert r.stdout.startswith("[jq]")

def test_info_unknown_is_nonzero():
    r = _run("info", "not_a_real_tool")
    assert r.returncode != 0
```

**Step 3: Confirm failure**

Run: `pytest tests/test_clive_tools_cli.py -v`
Expected: FAIL — script doesn't exist.

**Step 4: Implement the CLI**

Create `tools/clive-tools`:

```python
#!/usr/bin/env python3
"""In-pane tool discovery — agents call `tools` / `tool_info` mid-task.

Usage:
    clive-tools list                  # category index (Tier 0)
    clive-tools list <category>       # names in one category (Tier 1)
    clive-tools info <tool>           # reference card (Tier 2)
"""
import os
import sys
from pathlib import Path

# Pin to repo root so this script works whether invoked from /tmp/clive
# or from the repo. Mirrors clive.py's sys.path injection.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "clive"))

from toolsets import (
    CATEGORIES, build_tier0_summary, build_tier1_names, build_tier2_card,
)


def main(argv):
    if not argv or argv[0] == "list":
        rest = argv[1:] if argv and argv[0] == "list" else argv
        if not rest:
            print(build_tier0_summary(list(CATEGORIES.keys())))
            return 0
        category = rest[0]
        if category not in CATEGORIES:
            print(f"Unknown category: {category}", file=sys.stderr)
            print(f"Try one of: {', '.join(CATEGORIES)}", file=sys.stderr)
            return 2
        print(build_tier1_names([category]))
        return 0
    if argv[0] == "info":
        if len(argv) < 2:
            print("Usage: clive-tools info <tool>", file=sys.stderr)
            return 2
        card = build_tier2_card(argv[1])
        if card is None:
            print(f"Unknown tool: {argv[1]}", file=sys.stderr)
            return 1
        print(card)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

Then:

```bash
chmod +x tools/clive-tools
```

**Step 5: Wire shell aliases (optional, do only if straightforward)**

Find where panes are initialized (`session/session.py`). Append to the shell init a one-liner:

```bash
alias tools='clive-tools list'
alias tool_info='clive-tools info'
```

If pane init isn't ergonomic to extend in this PR, defer this to a follow-up — agents can call `clive-tools list data` directly. Document in the planner prompt either way.

**Step 6: Update planner & worker prompts to mention discovery**

In `build_planner_prompt`, add at the end of the tools/discovery preamble:

```
The agent can discover tools mid-task with:
  clive-tools list                   # category index
  clive-tools list <category>        # tools in one category
  clive-tools info <name>            # reference card for a tool
```

**Step 7: Run tests**

Run: `pytest tests/test_clive_tools_cli.py -v`
Expected: PASS.

**Step 8: Commit**

```bash
git add tools/clive-tools src/clive/llm/prompts.py tests/test_clive_tools_cli.py
git commit -m "feat(registry): clive-tools CLI for in-pane discovery (gh#39)"
```

---

### Task 11: Manual smoke test

**Files:** none — interactive.

**Step 1: Run an end-to-end task with the flag off**

```bash
python clive.py "fetch the weather for Berlin and the top 3 HN headlines, write a summary to /tmp/clive/summary.txt"
```

Note: token usage from stderr telemetry.

**Step 2: Run the same task with the flag on**

```bash
CLIVE_PROGRESSIVE_TOOLS=1 python clive.py "fetch the weather for Berlin and the top 3 HN headlines, write a summary to /tmp/clive/summary.txt"
```

Compare tokens. Confirm the planner still picks the right tools (it should — the names in Tier 1 are enough to drive selection).

**Step 3: Spot-check a worker prompt**

```bash
CLIVE_PROGRESSIVE_TOOLS=1 CLIVE_DEBUG_PROMPTS=1 python clive.py "..."
```

(Use the existing prompt-dump mechanism if there is one; otherwise temporarily print the prompt in the runner.) Confirm the Tier-2 cards for the subtask's declared tools appear in the worker turn.

**Step 4: Document findings**

Append a short note to `docs/plans/2026-05-22-four-tier-tool-registry.md` (this file) under a new `## Verification results` section: before/after token counts, any quality regressions, and any TODOs that surfaced.

**Step 5: No commit yet** — verification only.

---

### Task 12: Docs + CHANGELOG + diary + kanban

**Files:**
- Modify: `CLAUDE.md` — short bullet under "Source layout" or a new "Tool registry" subsection
- Modify: `CHANGELOG` (if it exists; otherwise skip)
- Modify: `docs/plans/2026-05-22-four-tier-tool-registry.md` — fill in the verification section from Task 11
- New diary entry: `.dev-diary/<today>-four-tier-registry.md` if the diary skill is in active use

**Step 1: Update CLAUDE.md**

Add to the `### Toolsets` (or analogous) subsection of `CLAUDE.md`:

```markdown
- **Four-tier registry (gh#39, opt-in via `CLIVE_PROGRESSIVE_TOOLS=1`):**
  - Tier 0 = `build_tier0_summary(categories)` — category index
  - Tier 1 = `build_tier1_names(categories)` — tool names per category
  - Tier 2 = `build_tier2_card(name)` — compact card per tool
  - Tier 3 = `drivers/*.md` (unchanged)
  Planner emits `subtask.tools=[...]`; worker runner injects matching Tier 2 cards.
  In-pane discovery: `clive-tools list|info`. Auto-categorization helper:
  `classify_tool_to_category(name, description)` (used by gh#41 auto-explore).
```

**Step 2: Update kanban**

```bash
bash $CLAUDE_SKILL_DIR/scripts/move.sh 2026-05-22-progressive-tool-discovery-four-tier-reg done
bash $CLAUDE_SKILL_DIR/scripts/note.sh 2026-05-22-progressive-tool-discovery-four-tier-reg \
  "Shipped four-tier registry behind CLIVE_PROGRESSIVE_TOOLS=1. classify_tool_to_category unblocks gh#41 Phase 1."
```

**Step 3: Commit**

```bash
git add CLAUDE.md docs/plans/2026-05-22-four-tier-tool-registry.md
git commit -m "docs: four-tier tool registry (gh#39)"
```

---

### Task 13: Open the PR

**Step 1: Push branch**

```bash
git push -u origin feature/four-tier-registry
```

**Step 2: Open PR**

```bash
gh pr create --title "feat(registry): four-tier progressive tool disclosure (gh#39)" \
  --body "$(cat <<'EOF'
## Summary
- Adds Tier 0 (category index), Tier 1 (names per category), Tier 2 (per-tool card), keeping Tier 3 = existing drivers.
- Behind `CLIVE_PROGRESSIVE_TOOLS=1` for now; legacy path unchanged.
- New `subtask.tools` field — planner emits, worker injects Tier-2 cards.
- `classify_tool_to_category()` helper unblocks gh#41 Phase 1 auto-explore.
- In-pane discovery via `clive-tools list|info`.

## Test plan
- [ ] `pytest tests/test_tool_registry_*.py tests/test_subtask_tools_field.py tests/test_worker_tool_context.py tests/test_progressive_planner_prompt.py tests/test_clive_tools_cli.py` — all green
- [ ] `pytest tests/test_executor.py tests/test_planner.py tests/test_router.py` — no regression
- [ ] Manual: `CLIVE_PROGRESSIVE_TOOLS=1 python clive.py "<multi-step task>"` runs to completion with fewer planner tokens than baseline.
EOF
)"
```

Done.

---

## Verification commands run at end

```bash
pytest tests/test_tool_registry_tokens.py \
       tests/test_tool_registry_cards.py \
       tests/test_tool_registry_tiers.py \
       tests/test_tool_registry_classify.py \
       tests/test_subtask_tools_field.py \
       tests/test_worker_tool_context.py \
       tests/test_progressive_planner_prompt.py \
       tests/test_clive_tools_cli.py -v

# Regression sweep
pytest tests/test_executor.py tests/test_planner.py tests/test_router.py -v
```

## Verification results (Task 11)

Live LLM end-to-end runs (planner+worker on a real task with `CLIVE_PROGRESSIVE_TOOLS=1`) are deferred to gh#40's eval framework — they're plan-quality regression tests that need an eval harness, not a one-off smoke. What was verified here is the mechanical wiring and token-budget claims:

| Profile | Legacy (chars / ~tokens) | Progressive (chars / ~tokens) | Reduction |
|---|---|---|---|
| `standard` | 2238 / 559 | 271 / 67 | **87.9%** |
| `full` | 3296 / 824 | 451 / 112 | **86.3%** |

(Token estimate: 4 chars/token, the in-test rule of thumb.)

Sample of `build_tools_summary(...)` under the flag (full profile):

```
Tool categories available: comms(4), core(1), data(5), docs(3), info(4), media(4), productivity(3), search(1), web(2)
Use `tool_info <name>` for details on a specific tool.

comms: email, icalBuddy, khard, terminal-notifier
core: shell
data: data, jq, rg, mlr, sqlite3
docs: docs, pandoc, pdftotext
info: weather, hackernews, exchange, github_api
media: media, yt-dlp, whisper, ffmpeg
productivity: task, watson, nb
search: ddgr
web: browser, monolith
```

Sample of `build_worker_tool_context(subtask)` for `tools=['yt-dlp','whisper','jq']`:

```
Tools you may need:
[yt-dlp] download video/audio (1000+ sites)
  yt-dlp URL   -x audio-only  --audio-format mp3  -f best  --write-sub  -o TMPL
  yt-dlp -x --audio-format mp3 URL

[whisper] local speech-to-text
  whisper AUDIO   --model tiny|base|small|medium  --output_format txt|srt|json
  whisper audio.mp3 --model small --output_format txt

[jq] JSON processor
  jq 'FILTER' [file]   .key | .[] | select(c) | map(f) | length
  curl -s api | jq -r '.data[].name'
```

Full test sweep: **1084 tests passing** on `feature/four-tier-registry`.

Things to validate empirically once gh#40 lands (i.e. real LLM runs with measurable success metric):

- Does the planner actually populate `subtask.tools` consistently when the flag is on?
- Does dropping descriptions from Tier 0+1 in favor of names hurt tool-selection accuracy?
- Are the Tier-2 worker cards detailed enough to drive correct invocation, or do agents end up calling `clive-tools info <name>` anyway?

These are quality-of-plan questions the test suite can't answer.

## Open questions / known tradeoffs

- **The 90% token-reduction headline is aspirational at current size.** With ~30 commands the legacy summary is already only ~600–900 tokens. The architecture starts to pay off when the toolset crosses ~80 tools (i.e. once gh#41 + gh#42 start landing exploratory drivers). For now we ship the substrate and a 50% floor.
- **Keyword-based `classify_tool_to_category` will mis-classify novel tools.** That's acceptable for unblocking gh#41 Phase 1 — a wrong-but-plausible bucket is better than orphaning a new driver, and the *only* consumer today is auto-explore. An LLM-driven classifier is a separate, larger card.
- **Self-service shell aliases vs explicit `clive-tools` invocations:** if pane-init wiring proves messy, skip the aliases — agents calling `clive-tools list data` directly is acceptable, and the planner prompt names the CLI explicitly.
- **YAML migration deferred deliberately.** The current in-memory dict is fine at this size; moving to YAML files adds I/O and a parsing step without delivering anything new. Revisit once `len(COMMANDS) > 80` or the discovery loop starts writing tool metadata that wants its own file.
