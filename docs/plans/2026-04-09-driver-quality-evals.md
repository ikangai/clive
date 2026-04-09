# Driver Quality & Eval Coverage Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Every driver prompt reliably enables a sub-agent (even a cheap/fast model) to launch, use, and complete tasks with its tool. Every driver has evals proving it works.

**Architecture:** Drivers are compact reference cards in `drivers/{app_type}.md` that get injected into the sub-agent's system prompt. The sub-agent reads the pane screen, decides what to do, sends keystrokes via `<cmd type="shell">`. Drivers must bridge the gap between "blank shell prompt" and "tool in use."

**Tech Stack:** Markdown driver files, JSON eval tasks, existing eval harness (`evals/harness/run_eval.py`).

---

## Driver Template Standard

Every driver MUST follow this structure (derived from the best drivers: shell.md, browser.md):

```markdown
# {Tool Name} Driver

ENVIRONMENT: {what the pane starts as — always a bash shell}
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
  {tool_command}  → {what it does}
  {tool_command}  → {what it does}

PATTERNS:
- {Common task}: {exact command or sequence}
- {Common task}: {exact command or sequence}

PITFALLS:
- {What goes wrong}: {how to handle it}

COMPLETION: Use <cmd type="task_complete">summary</cmd> when done.
Write results to /tmp/clive/ for other subtasks.
```

**Critical rules:**
- Always state the pane starts as a bash shell (the agent sees `[AGENT_READY] $`)
- Always show PRIMARY TOOLS with exact commands (not just key bindings)
- For TUI tools (neomutt, etc.): explain how to LAUNCH, what the INITIAL SCREEN looks like, and how to QUIT
- Keep under 50 lines — the driver gets stripped after turn 1 to save tokens
- Prefer shell commands over TUI navigation when both work (send_reply.sh > neomutt compose)

---

## Driver Audit

| Driver | File | Lines | Structure | Launch | Primary Tools | Patterns | Pitfalls | Status |
|--------|------|-------|-----------|--------|---------------|----------|----------|--------|
| shell | shell.md | 33 | ✓ | N/A (is shell) | ✓ | ✓ | ✓ | **Good** |
| browser | browser.md | 35 | ✓ | N/A (is shell) | ✓ | ✓ | ✓ | **Good** |
| data | data.md | 33 | ✓ | N/A (is shell) | ✓ | ✓ | ✓ | **Good** |
| docs | docs.md | 29 | ✓ | N/A (is shell) | ✓ | ✓ | ✓ | **Good** |
| media | media.md | 32 | ✓ | N/A (is shell) | ✓ | ✓ | ✓ | **Good** |
| email_cli | email_cli.md | 44 | ✓ | ✓ (fixed) | ✓ | ✓ | ✓ | **Fixed** |
| agent | agent.md | 38 | Custom | N/A (protocol) | Protocol | N/A | ✓ | **OK** (specialized) |
| default | default.md | 3 | Minimal | N/A | N/A | N/A | N/A | **OK** (fallback) |

**Verdict:** Shell-based drivers (shell, browser, data, docs, media) are solid — the pane IS a shell and the tools are CLI commands. The gap was in TUI-based drivers (email) where the agent must launch an app. Fixed for email. Agent and default are special cases.

---

## Eval Coverage Plan

### Existing Evals
- `layer2/shell/` — 4 tasks (find, count, pipeline, JSON)
- `layer2/cli_fundamentals/` — 20 tasks (echo, ls, cat, wc, grep, pipes, csv, json, curl)
- `layer2/creative_tasks/` — 15 tasks (haiku, reports, charts)
- `layer2/data/` — data processing tasks
- `layer2/lynx/` — browser tasks
- `layer2/tool_availability/` — 10 tasks (missing tool detection)
- `layer2/tool_config/` — 10 tasks (config system unit tests)
- `layer2/tool_config_classifier/` — 5 tasks (classifier mode tests)
- `layer2/email/` — 5 tasks (send, launch, special chars)

### Missing Eval Coverage

Each driver needs evals that test its **core workflows** — the 3-5 things the sub-agent must be able to do.

---

### Task 1: Media driver evals (`evals/layer2/media/`)

**Files:**
- Create: `evals/layer2/media/tasks.json`
- Create: `evals/layer2/media/fixtures/` (sample files)

**Core workflows to test:**
1. Get video info with ffprobe
2. Extract audio from video with ffmpeg
3. Convert image format with ImageMagick convert
4. Download video metadata with yt-dlp --print
5. Generate thumbnail from video with ffmpeg

**Fixtures needed:** A small test video (~100KB), a test image.

---

### Task 2: Data driver evals (`evals/layer2/data_processing/`)

**Files:**
- Create: `evals/layer2/data_processing/tasks.json`
- Create: `evals/layer2/data_processing/fixtures/` (sample CSV/JSON)

**Core workflows to test:**
1. Filter CSV rows with mlr
2. Extract JSON field with jq
3. Sort and count frequencies with sort/uniq
4. Convert CSV to JSON with mlr
5. Aggregate numeric column with awk

**Fixtures needed:** Sample CSV (10 rows), sample JSON array.

---

### Task 3: Docs driver evals (`evals/layer2/docs/`)

**Files:**
- Create: `evals/layer2/docs/tasks.json`

**Core workflows to test:**
1. Read a man page and extract info
2. Convert markdown to plain text with pandoc (if installed)
3. Count words/lines in a file
4. Diff two files
5. Search man pages by keyword

---

### Task 4: Browser driver evals (extend `evals/layer2/lynx/`)

**Core workflows to test (may already exist):**
1. Fetch a URL with lynx -dump
2. Extract links with lynx -listonly
3. Fetch JSON API with curl
4. Download a file with wget
5. Follow redirects with curl -L

---

### Task 5: Email driver evals (extend `evals/layer2/email/`)

**Additional tests beyond current 5:**
1. Send email with multi-line body
2. Read inbox and report count (requires configured account)
3. Search emails by sender
4. Forward an email

**Note:** Reading evals require a configured IMAP account. Tag these as `requires_config: true` so the harness can skip them when unconfigured.

---

### Task 6: Cross-driver eval (agent picks the right tool)

**File:** `evals/layer2/tool_routing/tasks.json`

**Tests that the agent routes to the right driver:**
1. "Convert this CSV to JSON" → should use data pane / jq/mlr
2. "Download this YouTube video" → should use media pane / yt-dlp
3. "Send an email to X" → should use email pane / send_reply.sh
4. "What does the grep command do?" → should use docs pane / man
5. "Fetch the weather" → should use browser pane / curl

---

### Task 7: Add `requires_config` field to eval harness

**Files:**
- Modify: `evals/harness/run_eval.py`

Some evals (email reading, cloud sync) need configured accounts. Add an optional `requires_config` field to task definitions. The harness skips tasks where the required config doesn't exist:

```json
{
  "id": "email_read_inbox_006",
  "requires_config": "email.toml",
  ...
}
```

Harness check:
```python
if task.get("requires_config"):
    from config import load_config
    if not load_config(task["requires_config"]):
        return EvalResult(task_id=..., passed=True, detail="skipped: no config")
```

---

### Task 8: Run all evals, identify failing drivers, iterate

**Process:**
1. Run: `python3 evals/harness/run_eval.py --layer 2 --all`
2. Identify failing tasks by driver
3. For each failing driver:
   - Read the eval audit trail (`.clive/audit/`)
   - Identify what the agent did wrong
   - Fix the driver prompt
   - Re-run the failing eval
4. Repeat until completion rate > 80% per driver

---

## Priority Order

1. **Email driver** — DONE (fixed driver + 5 evals)
2. **Data processing evals** — highest value, most common workflow
3. **Tool routing evals** — validates the classifier + expand system
4. **Media evals** — needs fixtures
5. **Docs evals** — straightforward
6. **Browser evals** — extend existing lynx evals
7. **requires_config harness** — enables config-dependent evals
8. **Run all + iterate** — the actual improvement loop
