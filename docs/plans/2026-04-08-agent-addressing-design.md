# Agent Addressing & Peer Conversation Design — `clive@host`

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable Clive instances to address each other using email-style `clive@host` syntax, communicate via a structured conversational protocol over SSH, and auto-detect whether the caller is a human (→ TUI) or another Clive (→ conversational mode). The initiator's LLM API key is forwarded via SSH, so the remote Clive runs on the caller's dime.

**Architecture:** The planner extracts `clive@<host>` from task text, resolves it via registry (`~/.clive/agents.yaml`) or auto-resolve, lazily injects an agent pane into the tmux session, and opens a persistent SSH session. The two Clive instances engage in a turn-by-turn peer dialogue using a structured protocol (`TURN:`, `QUESTION:`, `CONTEXT:`). The inner Clive auto-detects conversational mode via `isatty()`.

**Tech Stack:** SSH (with `SendEnv` for API key forwarding), tmux (libtmux), YAML config, existing agent protocol foundation (remote.py)

**Governing principle from SPEC.md:** *"LLM where judgment is required, shell everywhere else."* Cross-machine is the compelling use case — local parallelism is cheaper via the existing DAG scheduler.

---

## 1. Addressing & Parsing

When the user writes a task like *"ask clive@devbox to check disk usage"*, the planner detects the `clive@<host>` pattern and extracts two things: the target host (`devbox`) and the inner task (`check disk usage`).

**Regex:** `clive@([\w.\-]+)`

- Can appear anywhere in task text
- The planner strips the `clive@host` token before sending the task to the remote instance
- Multiple addresses in one task are valid: *"ask clive@gpu to render the video, then ask clive@web to upload it"* — the planner creates two subtasks with a dependency chain

**New file: `agents.py`**

```python
def parse_agent_addresses(task: str) -> list[tuple[str, str]]:
    """Extract clive@host addresses from task text.
    
    Returns list of (host, remaining_task) tuples.
    For single-agent tasks, returns one tuple.
    For multi-agent tasks, the planner splits by natural language cues.
    """
```

---

## 2. Resolution & Registry

When the planner finds `clive@devbox`, resolution is two steps:

1. **Check registry** — load `~/.clive/agents.yaml`, look for a matching host entry. If found, use its settings (SSH key, toolset, clive path, timeout).

2. **Auto-resolve fallback** — if no entry exists, construct a default: `ssh devbox 'cd ~ && python3 clive.py --conversational'`. This works for any host where the user has SSH key access and Clive is installed.

**Registry format:**

```yaml
# ~/.clive/agents.yaml
devbox:
  host: devbox.local          # optional, defaults to the name itself
  toolset: web                 # what the remote clive runs with
  path: /opt/clive/clive.py   # non-standard install location
  key: ~/.ssh/devbox_key      # SSH key
  timeout: 120                # max seconds per conversation turn

localhost:
  toolset: web
```

**Resolution output:** a pane definition dict — the same format that `PANES` in toolsets.py uses. This dict is injected into the session dynamically. No need to predefine panes for every possible agent.

```python
def resolve_agent(host: str) -> dict:
    """Resolve a clive@host address to a pane definition.
    
    Checks ~/.clive/agents.yaml first, falls back to auto-resolve.
    Returns a dict compatible with PANES entries in toolsets.py.
    """
```

---

## 3. Bring Your Own LLM (SSH Env Forwarding)

The initiating Clive's API key is forwarded to the remote instance via SSH environment variables. The caller pays — this keeps the cost model simple and lets "dumb" remote machines run Clive without their own API keys.

**Mechanism:** SSH `SendEnv` directive.

The outer Clive's SSH command includes:

```bash
ssh -o SendEnv=ANTHROPIC_API_KEY \
    -o SendEnv=OPENAI_API_KEY \
    -o SendEnv=OPENROUTER_API_KEY \
    -o SendEnv=LLM_PROVIDER \
    -o SendEnv=AGENT_MODEL \
    host 'python3 clive.py --conversational'
```

**Requirement:** The remote host's `sshd_config` must have `AcceptEnv` configured:

```
# /etc/ssh/sshd_config on remote
AcceptEnv ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY LLM_PROVIDER AGENT_MODEL
```

**Fallback:** If env forwarding isn't configured, the remote Clive uses whatever API key is in its own `.env` file. This is intentional — some deployments want the remote machine to use its own key (shared team key, different provider, etc.).

The `build_agent_ssh_cmd()` function in `agents.py` detects which provider env vars are set locally and adds `SendEnv` for each.

---

## 4. Mode Auto-Detection

The inner Clive detects who's connecting and selects the appropriate interface:

| Caller | Detection | Inner Clive Mode | Interface |
|--------|-----------|------------------|-----------|
| Another Clive | no TTY (auto) or `--conversational` | Conversational | `TURN:`, `QUESTION:`, `CONTEXT:` protocol |
| Human via SSH | TTY present (auto) or `--tui` | TUI | Full textual UI |
| Script/CI | `--quiet --json` | One-shot | JSON result on stdout |

**Selection logic in `clive.py`:**

```python
if args.conversational:
    mode = "conversational"
elif args.tui:
    mode = "tui"
elif args.quiet or args.json:
    mode = "quiet"
elif not sys.stdin.isatty():
    mode = "conversational"    # no TTY → Clive calling
else:
    mode = "tui" if has_tui else "default"
```

**Why this works:** When the outer Clive runs `ssh host 'clive "task"'` (no `-t` flag), the remote process has no TTY → auto-conversational. When a human runs `ssh host` then `clive`, the TTY is present → TUI.

The `--conversational` and `--tui` flags exist for testing and overrides.

---

## 5. Conversational Protocol

The inner Clive in conversational mode writes structured lines to stdout. The outer Clive reads these from the tmux pane screen.

**Protocol markers:**

```
TURN: thinking      — I'm working, don't type
TURN: waiting       — your move, I need input
TURN: done          — task complete
TURN: failed        — task failed

CONTEXT: {"key": "value", ...}    — structured state snapshot
QUESTION: "free text question"     — specific question for the peer
PROGRESS: step N of M — desc       — status update (same as today)
FILE: filename                      — file available for transfer (same as today)
```

**Flow of a typical conversation:**

```
OUTER types:  read HN and summarize anthropic mythos
INNER prints: TURN: thinking
INNER prints: PROGRESS: step 1 of 2 — fetching HN front page
INNER prints: CONTEXT: {"anthropic_mentions": 2, "titles": ["Claude 4 launch", "Safety paper"]}
INNER prints: QUESTION: "Found 2 Anthropic articles. Summarize both or focus on one?"
INNER prints: TURN: waiting
                                              ← outer LLM reads screen, reasons
OUTER types:  both, but emphasize the safety paper
INNER prints: TURN: thinking
INNER prints: PROGRESS: step 2 of 2 — reading and summarizing
INNER prints: FILE: anthropic_summary.txt
INNER prints: CONTEXT: {"result": "Anthropic published two notable items today..."}
INNER prints: TURN: done
```

**Key rule:** The outer Clive only types when it sees `TURN: waiting`. On `TURN: thinking`, it skips the LLM call entirely (saves tokens). On `TURN: done` or `TURN: failed`, it terminates.

**Backward compatibility:** The existing `DONE:`, `PROGRESS:`, and `FILE:` markers from `remote.py` remain supported. `TURN: done` is the new preferred form; `DONE: {...}` still works for one-shot `--quiet --json` mode.

---

## 6. Inner Clive — Conversational Mode

In conversational mode, the inner Clive:

1. **Reads one line from stdin** as the initial task
2. **Plans and executes normally** — but wraps output in protocol markers
3. **When it needs input** — emits `QUESTION:` + `CONTEXT:` + `TURN: waiting`, then blocks on `sys.stdin.readline()`
4. **Reads the response**, incorporates it, continues execution
5. **When finished** — emits final `CONTEXT:` + `TURN: done`

**Output skin:** Today, `output.py` has two modes: normal (stdout with ANSI) and quiet (stderr). Conversational mode is a third skin:

```python
# In output.py
_conversational = False

def set_conversational(enabled: bool):
    global _conversational
    _conversational = enabled

# When _conversational is True:
# progress(msg) → prints "PROGRESS: {msg}"
# step(msg)     → prints "PROGRESS: {msg}"  (no ANSI, no pulse animation)
# detail(msg)   → prints "PROGRESS:   {msg}"
# result(msg)   → prints "CONTEXT: {json}" + "TURN: done"
```

**When to ask:** The inner Clive's LLM decides whether to ask or proceed. The system prompt instructs: *"You're in a peer conversation with another Clive instance. If you're uncertain about scope, priorities, or interpretation, ask via QUESTION:. If you have enough context to proceed, proceed. Don't ask unnecessary questions."*

---

## 7. Dynamic Pane Injection

Today all panes are created at session startup in `setup_session()`. With `clive@` addressing, panes appear lazily when the planner first targets an agent.

**Flow:**

1. **Planner** finds `clive@devbox`, calls `resolve_agent("devbox")`
2. Returns a pane definition dict
3. **Before execution**, `ensure_agent_pane(session, panes, "devbox", config)` checks if `agent-devbox` exists. If not: creates a new tmux window, opens the SSH connection (without `-t`), waits for the connection, adds to the `panes` dict
4. **Subtask** gets `pane: "agent-devbox"`

**Pane naming:** `agent-<host>` — e.g., `agent-localhost`, `agent-devbox`, `agent-prod`. Avoids collision with existing pane names. Easy to identify in tmux.

**Lifecycle:** Panes are created lazily and persist for the session. Reused if a second task targets the same agent. Cleaned up with the session at the end.

```python
# In session.py
def ensure_agent_pane(session, panes, host, config):
    """Lazily create an agent pane for clive@host if it doesn't exist.
    
    Creates a new tmux window, opens SSH (no -t), waits for connection.
    Adds to panes dict. Returns PaneInfo.
    """
```

---

## 8. Executor Changes

The executor's `run_subtask()` loop (around line 1038 in `executor.py`) currently short-circuits on `DONE:` for agent panes. The conversational model replaces this with turn-state-aware handling:

```python
if pane_info.app_type == "agent":
    turn_state = parse_turn_state(screen)   # new function in remote.py
    
    if turn_state == "done":
        # Parse final CONTEXT: line → SubtaskResult(COMPLETED)
        
    elif turn_state == "failed":
        # Parse final CONTEXT: line → SubtaskResult(FAILED)
        
    elif turn_state == "thinking":
        # Inner Clive is working — skip LLM call, just wait
        continue
        
    elif turn_state == "waiting":
        # Inner Clive wants input — fall through to the normal
        # LLM reasoning loop below. The LLM reads QUESTION:/CONTEXT:
        # on screen, reasons, and types a response.
        pass
```

**Token savings:** `TURN: thinking` skips the LLM call entirely. The outer Clive only invokes its LLM when the inner Clive is actually waiting for a response. In a 10-turn conversation where 6 turns are `thinking`, this saves 6 LLM calls.

**Turn budget:** The planner sets `max_turns` per subtask. Each `waiting` → response cycle counts as one turn. `thinking` cycles don't count — they're just polling.

---

## 9. Outer Clive — Agent Driver Prompt

The `drivers/agent.md` file is rewritten for the conversational protocol:

```markdown
# Agent Driver (clive-to-clive peer conversation)

ENVIRONMENT: connected to a remote clive instance via SSH.
The remote clive runs in conversational mode (structured protocol).

PROTOCOL (read from screen):
  TURN: thinking    — remote is working. WAIT. Do not type.
  TURN: waiting     — remote needs your input. Read QUESTION/CONTEXT, respond.
  TURN: done        — task complete. Parse final CONTEXT for result.
  TURN: failed      — task failed. Parse CONTEXT for error details.

  CONTEXT: {...}    — structured state from remote
  QUESTION: "..."   — specific question from remote
  PROGRESS: ...     — status update (informational)
  FILE: filename    — file available for transfer

RULES:
- ONLY type when you see TURN: waiting
- Read the QUESTION and CONTEXT lines before responding
- Keep responses concise and actionable
- You are a peer, not a supervisor — the remote clive has its own judgment
- If the result in TURN: done is insufficient, you can send a follow-up task

COMPLETION: Report the final CONTEXT as your subtask result.
```

---

## 10. Files Changed

| File | Change |
|------|--------|
| **New: `agents.py`** | `parse_agent_addresses(task)` — regex extraction. `resolve_agent(host)` — registry + auto-resolve. `build_agent_ssh_cmd(host, config)` — SSH command with `SendEnv` for API keys, no `-t` flag |
| **`clive.py`** | Add `--conversational` flag. Auto-detection logic (`isatty()`). Conversational main loop: read stdin → execute → emit protocol → wait for input → loop |
| **`remote.py`** | Add `parse_turn_state(screen)` — extracts latest `TURN:` line. Existing `DONE:`/`PROGRESS:`/`FILE:` parsers kept for backward compat |
| **`executor.py`** | Update agent pane handling in `run_subtask()`: `thinking` → skip LLM, `waiting` → fall through to interactive loop, `done`/`failed` → terminate with result |
| **`output.py`** | Add conversational output skin. `set_conversational(True)` makes `progress()`/`step()` emit `PROGRESS:` lines. New helpers for `TURN:`/`CONTEXT:`/`QUESTION:` |
| **`drivers/agent.md`** | Rewrite for conversational protocol — instruct outer LLM: only type on `TURN: waiting`, read CONTEXT/QUESTION |
| **`planner.py`** | Call `parse_agent_addresses(task)` before planning. Route subtasks to `agent-<host>` panes. Set `max_turns` per subtask (caller controls budget) |
| **`session.py`** | Add `ensure_agent_pane(session, panes, host, config)` for lazy pane creation |
| **`~/.clive/agents.yaml`** | New optional config file — per-host settings |

**Files unchanged:** `models.py`, `completion.py`, `toolsets.py`

**Cleanup after shipping:** Remove `localhost_agent` pane, `loopback` category/profile from `toolsets.py` — `clive@localhost` replaces them.

---

## 11. Relationship to Existing Code

**What stays:**
- `--remote` flag (one-shot SSH, no conversation) — kept for simple tasks and CI
- `--quiet --json` mode — kept for scripts and backward compat
- `DONE:` protocol parsing in `remote.py` — still works for one-shot mode
- `PaneAgent` / `SharedBrain` in `pane_agent.py` — agent pane state management applies here too
- File transfer via `scp_files_from_result()` — used after `TURN: done` when `FILE:` lines are present

**What evolves:**
- The `remote_agent` pane definition in `toolsets.py` becomes a template/example — real agents are defined via `clive@host` addressing or `agents.yaml`
- `executor.py` agent handling moves from fire-and-forget to turn-aware
- `drivers/agent.md` evolves from one-shot protocol docs to peer conversation docs
- `output.py` gains a third output mode alongside normal and quiet

**What's new:**
- `agents.py` — addressing, resolution, SSH command building
- `--conversational` flag and `isatty()` auto-detection
- `TURN:` protocol (superset of existing `DONE:` protocol)
- Conversational output skin in `output.py`
- `ensure_agent_pane()` in `session.py`
- `~/.clive/agents.yaml` registry

---

## 12. Example End-to-End Scenarios

### Scenario A: Simple delegation
```
User runs:  clive "ask clive@prod to check disk usage"

1. Planner extracts: host=prod, task="check disk usage"
2. resolve_agent("prod") → checks agents.yaml → auto-resolve fallback
3. ensure_agent_pane() → creates tmux window, SSH to prod (SendEnv keys)
4. Outer types task into agent-prod pane
5. Inner Clive (conversational mode): 
     TURN: thinking → PROGRESS: checking df → CONTEXT: {"/": "82%"} → TURN: done
6. Outer reads TURN: done, parses CONTEXT → SubtaskResult
7. Summarizer reports: "Disk usage on prod: / is at 82%"
```

### Scenario B: Peer conversation with clarification
```
User runs:  clive "ask clive@research to find papers on transformer scaling laws"

1-4. Same as above
5. Inner Clive:
     TURN: thinking
     PROGRESS: searching arxiv
     CONTEXT: {"papers_found": 47, "date_range": "2020-2025"}
     QUESTION: "Found 47 papers. Want recent (2024+) only, or comprehensive?"
     TURN: waiting
6. Outer LLM reads screen, reasons, types: "recent only, top 10 by citations"
7. Inner Clive:
     TURN: thinking
     PROGRESS: filtering and ranking
     FILE: scaling_laws_top10.md
     CONTEXT: {"result": "Top 10 papers on transformer scaling...", "file": "scaling_laws_top10.md"}
     TURN: done
8. Outer reads result, triggers scp for the file
```

### Scenario C: Multi-agent pipeline
```
User runs:  clive "ask clive@scraper to get today's HN front page, then ask clive@writer to summarize it"

1. Planner creates two subtasks with dependency:
   - [1] pane=agent-scraper: "get today's HN front page"
   - [2] pane=agent-writer, depends_on=[1]: "summarize this: {result of 1}"
2. Both panes injected lazily
3. Subtask 1 runs → TURN: done with HN data
4. Subtask 2 runs with result injected → TURN: done with summary
```

### Scenario D: Human SSH to remote Clive
```
Human runs: ssh research-box
Then:       clive

→ isatty() returns True → TUI launches
→ Human interacts via full textual UI
→ Human's own .env on remote provides API key (or they set it up)
```
