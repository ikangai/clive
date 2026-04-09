# Tiered Intent Classification & Tool-Specific Routing

**Date:** 2026-04-08
**Status:** Approved

## Problem

Clive currently routes every task through the full planner (expensive LLM call) and script generator (second LLM call), even for trivial commands like `curl ikangai.com`. This adds 10-15s latency and wastes tokens. The architecture needs a fast path that resolves simple tasks without LLM involvement, and a classification layer that routes single-tool tasks directly to specialized executors.

## Design

### Three-Tier Intent Resolution

```
User Input
    |
    +- Tier 0: Regex Scanner (~0ms, zero cost)
    |   Pattern-match known CLI commands
    |   Match -> pre-flight checks -> direct execution
    |   No match -> Tier 1
    |
    +- Tier 1: Fast Classifier (Gemini Flash, ~1-2s, ~200 tokens)
    |   Receives: user input + available tools/panes list
    |   Returns: {mode, tool, cmd, pane, driver, fallback_mode, stateful, message}
    |   Routes:
    |     mode: "direct"       -> pre-flight -> execute cmd in pane
    |     mode: "script"       -> generate script with tool-specific driver
    |     mode: "interactive"  -> turn loop with driver (mutt, lynx, etc.)
    |     mode: "plan"         -> Tier 2 (complex multi-step)
    |     mode: "unavailable"  -> report missing tool + install hint
    |     mode: "answer"       -> respond directly, no execution
    |     mode: "clarify"      -> ask user for more info
    |
    +- Tier 2: Full Planner (main model, ~3-5s, ~1000 tokens)
        Only for genuinely complex multi-step tasks
        DAG decomposition -> parallel execution -> summarize
```

### Classifier Schema

The classifier (Gemini Flash via OpenRouter) receives compact context:

```json
{
  "input": "write an email to mt@ikangai.com",
  "available_tools": ["shell", "curl", "jq", "rg"],
  "available_panes": ["shell"],
  "installed_commands": ["curl", "jq", "rg", "grep"],
  "missing_commands": ["mutt", "icalBuddy", "lynx"]
}
```

Response schema:

```json
{
  "mode": "direct|script|interactive|plan|unavailable|answer|clarify",
  "tool": "curl",
  "pane": "shell",
  "driver": "shell",
  "cmd": "curl -sL ikangai.com",
  "fallback_mode": "script",
  "stateful": false,
  "message": null
}
```

### Pre-flight Validation

Before direct execution, validate without executing:
- `command -v {tool}` — binary exists
- `test -f {input_file}` — input files exist (parsed from cmd)
- Optional: `ping -c1 -W1 {host}` — host reachable (curl/ssh)
- Skip validation for `stateful: true` commands (only check binary)

On pre-flight failure: report specific error, no LLM call wasted.

### Fallback Chain

If direct execution fails (non-zero exit) and `fallback_mode` is set, automatically retry with that mode. Example: classifier returns `{mode: "direct", fallback_mode: "script"}` — on failure, generate a script to handle edge cases.

### Tool Availability Detection

The classifier receives installed/missing command lists. When a required tool is missing:
- Returns `mode: "unavailable"` with install instructions
- No execution attempted, instant feedback

## Implementation

### Files Changed

| File | Change |
|------|--------|
| `llm.py` | Add `CLASSIFIER_MODEL` (default: `google/gemini-3-flash-preview`) |
| `prompts.py` | Add `build_classifier_prompt()` |
| `clive.py` | Replace `_is_trivial` routing with 3-tier pipeline |
| `executor.py` | Add pre-flight validation to `run_subtask_direct` |
| `models.py` | Add `ClassifierResult` dataclass |

### Files Unchanged

Drivers, toolsets, session, completion, planner (stays as Tier 2).

## Eval Coverage

- **Tier 0:** 20 regex/direct tasks (cli_fundamentals, 100% baseline)
- **Tier 1:** ~15 classifier-routed tasks (single tool, availability checks)
- **Tier 2:** ~5 multi-step planner tasks (creative, multi-tool)
- **Pre-flight:** ~5 validation tasks (missing files, bad commands)
- **Total:** ~45 eval tasks
