# Repository Restructure Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restructure the clive repository from a flat 55-Python-files-in-root layout to a clean `src/clive/` package structure, remove tracked content that doesn't belong in the repo, and update all imports, tests, CI, and launcher scripts.

**Architecture:** Create a `src/clive/` package with subpackages for logical groupings (execution, observation, networking, tui, evolution). Move all Python source into the package. Use a `conftest.py` with `sys.path` insertion so tests keep working with `from module import X` style imports (no mass rewrite of import statements in source files — internal imports stay flat via `__init__.py` re-exports or the package being on sys.path). Remove blog posts and spec drafts from the tracked tree. Update install.sh, CI, and launcher.

**Tech Stack:** Python 3, pytest, git, bash

---

## Current State Analysis

### The Problem

55 Python files sit in the project root alongside markdown, shell scripts, config, and content files. The root directory has 103 entries. This is not presentable:

```
.
├── agents.py               (1 of 55 .py files in root)
├── agents_doctor.py
├── blog-03-*.md             (11 blog posts — tracked in git)
├── blog-04-*.md
├── ...
├── SPEC.md                  (spec drafts — internal docs)
├── SPEC-v3.md
├── agents-addressing-agents.md
├── autoresearch-results*.tsv (experiment results — partially gitignored)
├── clive.py
├── ... (50 more .py files)
├── tui.py
├── drivers/
├── evals/
├── selfmod/
├── server/
├── tests/               (72 test files)
└── tools/
```

### What Needs to Happen

1. **Remove content that doesn't belong**: 11 blog posts, 2 spec drafts, 1 stale addressing doc
2. **Gitignore transient files**: autoresearch results, .DS_Store patterns
3. **Create package structure**: `src/clive/` with subpackages
4. **Move source files**: 55 .py files from root into package
5. **Update all imports**: tests, CI, launchers
6. **Move non-Python assets**: drivers, tools, skills into the package or a data dir

### Target Structure

```
clive/
├── README.md
├── CHANGELOG.md
├── TOOLS.md
├── requirements.txt
├── install.sh
├── .env.example
├── .gitignore
├── .github/workflows/
│
├── src/clive/
│   ├── __init__.py
│   ├── __main__.py          (entry point: python -m clive)
│   ├── cli.py               (was clive.py)
│   ├── cli_args.py
│   ├── cli_handlers.py
│   ├── cli_modes.py
│   ├── core.py              (was clive_core.py)
│   ├── models.py
│   ├── config.py
│   ├── output.py
│   ├── router.py
│   │
│   ├── llm/
│   │   ├── __init__.py      (re-exports: get_client, chat, chat_stream, chat_with_tools, MODEL, etc.)
│   │   ├── client.py        (was llm.py)
│   │   ├── prompts.py
│   │   ├── tool_defs.py
│   │   └── delegate.py      (was delegate_client.py)
│   │
│   ├── planning/
│   │   ├── __init__.py
│   │   ├── planner.py
│   │   ├── dag_scheduler.py
│   │   └── summarizer.py
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── executor.py
│   │   ├── runtime.py
│   │   ├── script_runner.py
│   │   ├── interactive_runner.py
│   │   ├── planned_runner.py
│   │   ├── toolcall_runner.py
│   │   └── skill_runner.py
│   │
│   ├── observation/
│   │   ├── __init__.py
│   │   ├── classifier.py    (was observation.py)
│   │   ├── completion.py
│   │   ├── screen_diff.py
│   │   ├── context_compress.py
│   │   ├── command_extract.py
│   │   └── streaming_extract.py
│   │
│   ├── session/
│   │   ├── __init__.py
│   │   ├── manager.py       (was session.py)
│   │   ├── toolsets.py
│   │   ├── commands.py
│   │   └── file_inspect.py
│   │
│   ├── networking/
│   │   ├── __init__.py
│   │   ├── agents.py
│   │   ├── agents_doctor.py
│   │   ├── registry.py
│   │   ├── dashboard.py
│   │   ├── protocol.py
│   │   ├── remote.py
│   │   ├── ipc.py
│   │   └── coordinator.py
│   │
│   ├── tui/
│   │   ├── __init__.py
│   │   ├── app.py           (was tui.py)
│   │   ├── actions.py       (was tui_actions.py)
│   │   ├── commands.py      (was tui_commands.py)
│   │   ├── helpers.py       (was tui_helpers.py)
│   │   ├── task_runner.py   (was tui_task_runner.py)
│   │   └── theme.py         (was tui_theme.py)
│   │
│   ├── evolution/
│   │   ├── __init__.py
│   │   ├── evolve.py
│   │   ├── fitness.py       (was evolve_fitness.py)
│   │   └── mutate.py        (was evolve_mutate.py)
│   │
│   ├── selfmod/             (already a package — move as-is)
│   ├── server/              (already a package — move as-is)
│   ├── sandbox/             (already a package — move as-is)
│   │
│   ├── skills/              (was skills/ dir — skill .md files)
│   ├── drivers/             (was drivers/ dir — driver .md files)
│   ├── tools/               (was tools/ dir — helper shell scripts)
│   │
│   ├── skills.py            (skill loader)
│   ├── session_store.py
│   ├── scheduler.py
│   └── tool_schemas.py
│
├── tests/                   (stays at root level — standard practice)
│   ├── conftest.py          (NEW: adds src/clive to sys.path)
│   └── test_*.py            (72 test files — no import changes needed)
│
├── evals/                   (stays at root level)
│
├── docs/
│   ├── byollm-delegate.md
│   ├── deployment/
│   └── plans/
│
└── .clive/                  (governance data)
```

### Import Strategy: Why NOT Rewrite All Imports

The codebase has ~300 cross-module imports using flat names (`from models import Subtask`, `from llm import chat`). Rewriting all of them to package-qualified names (`from clive.models import Subtask`) in one PR would:
- Touch every single file
- Create an unreadable diff
- Break every in-flight branch
- Risk subtle import bugs

**Instead**: Use `conftest.py` for tests and adjust `sys.path` in the entry points (`__main__.py`, install.sh launcher). Internal imports within `src/clive/` work flat because Python adds the package directory to `sys.path` when running as a package. For the long term, imports can be gradually migrated to package-qualified names.

---

## Task 1: Remove Blog Posts and Stale Content

Remove tracked files that don't belong in a code repository.

**Files to remove from git:**
- `blog-03-agents-addressing-agents.md`
- `blog-04-tmux-as-agent-runtime.md`
- `blog-05-script-to-conversation.md`
- `blog-06-agents-that-learn.md`
- `blog-07-knowing-when-its-done.md`
- `blog-08-measuring-agent-quality.md`
- `blog-09-breeding-better-prompts.md`
- `blog-10-when-a-cli-becomes-a-server.md`
- `blog-11-the-code-that-rewrites-itself.md`
- `blog-12-naming-the-agents.md`
- `blog-13-stations-and-minds.md`
- `agents-addressing-agents.md` (superseded by agents.py + docs)
- `SPEC.md` (internal architecture spec, move to docs/)
- `SPEC-v3.md` (draft, move to docs/)

**Files to add to .gitignore:**
- `autoresearch-results*.tsv` (experiment artifacts — already partially ignored but pattern is incomplete)
- `.autoresearch_verify.py`
- `*.prev*.tsv`
- `.DS_Store` (already present but add recursive pattern)

**Step 1: Remove blog posts from git tracking**

```bash
git rm blog-*.md agents-addressing-agents.md
```

**Step 2: Move spec files to docs/**

```bash
git mv SPEC.md docs/SPEC.md
git mv SPEC-v3.md docs/SPEC-v3.md
```

**Step 3: Update .gitignore**

Add to `.gitignore`:
```
autoresearch-results*.tsv
*.prev*.tsv
.autoresearch_verify.py
**/.DS_Store
```

Remove the now-redundant entries that were already there for the same files.

**Step 4: Remove .DS_Store from tracking**

```bash
git rm --cached .DS_Store docs/.DS_Store 2>/dev/null
```

**Step 5: Commit**

```bash
git add -A
git commit -m "cleanup: remove blog posts, move specs to docs/, update .gitignore"
```

---

## Task 2: Create Package Structure and Move Source Files

Create the `src/clive/` directory tree and move all Python source files.

**Step 1: Create directory structure**

```bash
mkdir -p src/clive/{llm,planning,execution,observation,session,networking,tui,evolution}
```

**Step 2: Move files to their packages**

Use `git mv` for every file to preserve history:

```bash
# Entry points / CLI
git mv clive.py src/clive/cli.py
git mv clive_core.py src/clive/core.py
git mv cli_args.py src/clive/cli_args.py
git mv cli_handlers.py src/clive/cli_handlers.py
git mv cli_modes.py src/clive/cli_modes.py
git mv models.py src/clive/models.py
git mv config.py src/clive/config.py
git mv output.py src/clive/output.py
git mv router.py src/clive/router.py

# LLM
git mv llm.py src/clive/llm/client.py
git mv prompts.py src/clive/llm/prompts.py
git mv tool_defs.py src/clive/llm/tool_defs.py
git mv delegate_client.py src/clive/llm/delegate.py
git mv tool_schemas.py src/clive/llm/tool_schemas.py

# Planning
git mv planner.py src/clive/planning/planner.py
git mv dag_scheduler.py src/clive/planning/dag_scheduler.py
git mv summarizer.py src/clive/planning/summarizer.py

# Execution
git mv executor.py src/clive/execution/executor.py
git mv runtime.py src/clive/execution/runtime.py
git mv script_runner.py src/clive/execution/script_runner.py
git mv interactive_runner.py src/clive/execution/interactive_runner.py
git mv planned_runner.py src/clive/execution/planned_runner.py
git mv toolcall_runner.py src/clive/execution/toolcall_runner.py
git mv skill_runner.py src/clive/execution/skill_runner.py

# Observation
git mv observation.py src/clive/observation/classifier.py
git mv completion.py src/clive/observation/completion.py
git mv screen_diff.py src/clive/observation/screen_diff.py
git mv context_compress.py src/clive/observation/context_compress.py
git mv command_extract.py src/clive/observation/command_extract.py
git mv streaming_extract.py src/clive/observation/streaming_extract.py

# Session
git mv session.py src/clive/session/manager.py
git mv toolsets.py src/clive/session/toolsets.py
git mv commands.py src/clive/session/commands.py
git mv file_inspect.py src/clive/session/file_inspect.py

# Networking
git mv agents.py src/clive/networking/agents.py
git mv agents_doctor.py src/clive/networking/agents_doctor.py
git mv registry.py src/clive/networking/registry.py
git mv dashboard.py src/clive/networking/dashboard.py
git mv protocol.py src/clive/networking/protocol.py
git mv remote.py src/clive/networking/remote.py
git mv ipc.py src/clive/networking/ipc.py
git mv coordinator.py src/clive/networking/coordinator.py

# TUI
git mv tui.py src/clive/tui/app.py
git mv tui_actions.py src/clive/tui/actions.py
git mv tui_commands.py src/clive/tui/commands.py
git mv tui_helpers.py src/clive/tui/helpers.py
git mv tui_task_runner.py src/clive/tui/task_runner.py
git mv tui_theme.py src/clive/tui/theme.py

# Evolution
git mv evolve.py src/clive/evolution/evolve.py
git mv evolve_fitness.py src/clive/evolution/fitness.py
git mv evolve_mutate.py src/clive/evolution/mutate.py

# Remaining root-level modules
git mv skills.py src/clive/skills.py
git mv session_store.py src/clive/session_store.py
git mv scheduler.py src/clive/scheduler.py

# Existing packages
git mv selfmod src/clive/selfmod
git mv server src/clive/server
git mv sandbox src/clive/sandbox

# Data directories
git mv drivers src/clive/drivers
git mv skills src/clive/skills_data
git mv tools src/clive/tools
```

**Step 3: Move non-Python root assets**

```bash
git mv fetch_emails.sh src/clive/tools/fetch_emails.sh
git mv send_reply.sh src/clive/tools/send_reply.sh
```

**Step 4: Commit the move (no code changes yet — just renames)**

```bash
git add -A
git commit -m "refactor: move source into src/clive/ package structure"
```

---

## Task 3: Create Package __init__.py Files and Import Shims

Create `__init__.py` for every package and subpackage. Each `__init__.py` re-exports the public API so that existing flat imports (`from llm import chat`) continue to work when `src/clive/` is on `sys.path`.

**Step 1: Create `src/clive/__init__.py`**

```python
"""clive — CLI Live Environment."""
```

**Step 2: Create `src/clive/__main__.py`**

```python
"""Entry point for `python -m clive`."""
import sys
import os

# Add src/clive to sys.path so flat imports work
sys.path.insert(0, os.path.dirname(__file__))

from cli import main
main()
```

Note: This requires `clive.py` (now `cli.py`) to have a `main()` function. Check and wrap its `if __name__` block if needed.

**Step 3: Create subpackage `__init__.py` files with re-exports**

Each `__init__.py` re-exports the module's public symbols so that:
- `from llm import chat` works (finds `src/clive/llm/__init__.py` which re-exports from `client.py`)
- `from observation import ScreenClassifier` works (finds `src/clive/observation/__init__.py`)

Example for `src/clive/llm/__init__.py`:
```python
from .client import get_client, chat, chat_stream, chat_with_tools, MODEL, SCRIPT_MODEL, CLASSIFIER_MODEL, PROVIDER_NAME, PROVIDERS
from .delegate import DelegateClient
from .tool_defs import PANE_TOOLS, parse_tool_calls, tools_for_openai, tools_for_anthropic
from .prompts import (build_planner_prompt, build_script_prompt, build_interactive_prompt,
                      build_planned_prompt, build_classifier_prompt, build_summarizer_prompt,
                      build_triage_prompt, load_driver, load_driver_meta)
```

Similar for each subpackage. The `__init__.py` files are the critical piece that makes flat imports work.

**Step 4: Create `tests/conftest.py`**

```python
import sys
import os

# Add src/clive to sys.path so tests can use flat imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'clive'))
```

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor: add package __init__.py files and import shims"
```

---

## Task 4: Fix All Internal Imports

After the move, internal imports like `from llm import chat` need to work. Since all source is now under `src/clive/`, and we're running with `src/clive/` on `sys.path`, most flat imports will work IF the module names haven't changed.

**Renamed modules that need import updates:**

| Old name | New location | Module name on sys.path |
|----------|-------------|------------------------|
| `clive_core` | `src/clive/core.py` | `core` |
| `delegate_client` | `src/clive/llm/delegate.py` | needs re-export via `llm/__init__.py` |
| `observation` | `src/clive/observation/classifier.py` | needs re-export via `observation/__init__.py` |
| `session` | `src/clive/session/manager.py` | needs re-export via `session/__init__.py` |
| `tui` | `src/clive/tui/app.py` | needs re-export via `tui/__init__.py` |
| `tui_*` | `src/clive/tui/*.py` | needs re-exports |
| `evolve_fitness` | `src/clive/evolution/fitness.py` | needs re-export |
| `evolve_mutate` | `src/clive/evolution/mutate.py` | needs re-export |

For each renamed module, create a shim in the corresponding `__init__.py` that re-exports everything, OR create a compatibility module at the old name that imports from the new location.

**Alternative (simpler):** Don't rename files during the move. Keep `llm.py` as `llm.py` inside the package, `session.py` as `session.py`, etc. This eliminates ALL import changes for internal code. Only the `__init__.py` files are needed for subpackages because Python needs them to recognize directories as packages.

**This is the recommended approach. Re-do Task 2 file names:**

```
src/clive/llm/llm.py          (not client.py)
src/clive/llm/prompts.py
src/clive/llm/delegate_client.py  (not delegate.py)
...
src/clive/session/session.py   (not manager.py)
src/clive/tui/tui.py          (not app.py)
src/clive/observation/observation.py  (not classifier.py)
```

**IMPORTANT DECISION**: The implementer should decide between:
- **Option A (rename files)**: Cleaner names but requires updating ~300 import lines across all source files
- **Option B (keep names)**: Files keep their original names inside subpackages — zero import changes needed, just `__init__.py` re-exports

**Recommendation: Option B.** The gain from renaming is cosmetic; the cost is high (300+ import changes, all tested). The subpackage directory itself provides the semantic grouping.

**Step 1: Run the full test suite to verify everything works**

```bash
cd /Users/martintreiber/Documents/Development/clive
python3 -m pytest tests/ -x -q
```

**Step 2: Fix any broken imports found during testing**

Iterate until 746 tests pass.

**Step 3: Commit**

```bash
git commit -am "refactor: fix imports after package restructure"
```

---

## Task 5: Update Entry Points and Infrastructure

**Step 1: Update install.sh launcher**

Change the launcher script to invoke the package:
```bash
exec "${INSTALL_DIR}/.venv/bin/python3" -m clive "$@"
```
Or adjust the path:
```bash
exec "${INSTALL_DIR}/.venv/bin/python3" "${INSTALL_DIR}/src/clive/cli.py" "$@"
```

Similarly for the TUI launcher.

**Step 2: Update CI workflow**

In `.github/workflows/test.yml`, ensure the working directory or PYTHONPATH includes `src/clive`:
```yaml
- name: Run unit tests
  env:
    PYTHONPATH: src/clive
  run: python -m pytest tests/ -v --tb=short
```

**Step 3: Update `.github/workflows/eval.yml`** similarly

**Step 4: Update any hardcoded paths in source**

Search for references to file paths that assume root-level location:
- `drivers/` directory path in `prompts.py` (uses `__file__`-relative — should work)
- `skills/` directory path in `skills.py`
- `tools/` directory path
- `selfmod/` paths in `selfmod/constitution.py`

These all use `os.path.dirname(__file__)` relative paths, so they should work after the move. Verify each one.

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor: update entry points, CI, and paths for new structure"
```

---

## Task 6: Update README Project Structure

The README project structure section needs to reflect the new layout.

**Step 1: Update the project structure section in README.md**

Replace the flat file listing with the new hierarchical structure.

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: update README project structure for src/clive/ layout"
```

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Broken imports | Run full test suite (746 tests) after every step |
| Broken install.sh | Test launcher manually after change |
| Broken CI | Push to a branch, verify CI passes before merging |
| Lost git history | Use `git mv` exclusively — preserves blame |
| Evals break | Evals use `sys.path.insert(0, '.')` — update to `src/clive` |
| Skills/drivers path breaks | These use `__file__`-relative paths — verify |

## Execution Order

Tasks must be sequential (each depends on the previous):
1. Remove content (safe, no code changes)
2. Move files (git mv, no code changes)
3. Create __init__.py and conftest (make imports work)
4. Fix broken imports (test-driven)
5. Update infrastructure (install.sh, CI)
6. Update README

**Total estimated diff**: ~100 lines of new code (`__init__.py` files + conftest), 0 lines of source logic changed, 55 files moved.
