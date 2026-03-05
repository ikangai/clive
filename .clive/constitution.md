# Clive Self-Modification Constitution

Version: 1.0

## Purpose

This document governs all self-modifications to the clive codebase.
It is enforced by the deterministic gate and cannot be overridden by any LLM.

## File Tiers

Files are classified into tiers by risk level. Higher tiers require more
oversight before changes are applied.

### IMMUTABLE (never modified by selfmod)
- `selfmod/gate.py` — the deterministic gate itself
- `.clive/constitution.md` — this document
- `.clive/audit/*` — append-only audit trail

### GOVERNANCE (require Proposer + Reviewer + Auditor unanimous approval)
- `selfmod/*.py` — all selfmod modules (except gate.py)
- `.env` — environment configuration

### CORE (require Proposer + Reviewer approval)
- `clive.py` — main entry point
- `llm.py` — LLM client
- `executor.py` — task executor
- `planner.py` — task planner
- `session.py` — tmux session management
- `models.py` — data structures
- `prompts.py` — prompt templates

### STANDARD (require Proposer approval, Reviewer advisory)
- `tui.py` — terminal UI
- `toolsets.py` — tool definitions
- `completion.py` — shell completion
- `install.sh` — installer

### OPEN (Proposer can modify freely)
- `tools/*` — tool helper scripts
- `/tmp/clive/*` — temporary files
- Files not listed above

## Rules

1. **No modification may weaken the gate.** Any change that alters gate.py
   or this constitution is rejected unconditionally.

2. **No modification may disable the audit trail.** Every modification
   attempt must be logged, whether approved or rejected.

3. **No modification may escalate file tiers.** A change cannot move a file
   from a higher tier to a lower tier.

4. **Modifications must be atomic.** Either all files in a proposal are
   applied, or none are. Partial application is forbidden.

5. **The deterministic gate has final veto.** Even if all three LLM roles
   approve, the gate can reject based on pattern rules.

6. **Rollback must always be possible.** A git snapshot is taken before
   every modification. The user can undo with /undo.

7. **No network access during modification.** The selfmod pipeline operates
   only on local files. No external calls except to the LLM API.

8. **Rate limit: maximum 5 modifications per session.** Prevents runaway
   self-modification loops.

## Banned Patterns

The deterministic gate rejects any modification containing:
- `os.system(` or `subprocess.call(` with `shell=True`
- `eval(` or `exec(` with dynamic input
- Deletion of audit files
- Modification of gate.py or constitution.md
- `import ctypes` or `import importlib` with reload
- Obfuscated code (base64 encoded strings > 100 chars)
- Network calls in selfmod modules (urllib, requests, httpx, socket)
