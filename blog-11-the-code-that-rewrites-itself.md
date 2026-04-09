# The Code That Rewrites Itself

clive's toolset includes Claude Code, Codex, and aider. These are coding agents. They read source files, reason about them, and write new code. They work in tmux panes like every other tool — the LLM reads the screen, types commands, observes results.

The natural thing to ask one of these tools is: modify clive itself.

This is not hypothetical. clive's tool registry includes `ai` as a category. The shell pane has access to the full filesystem, including clive's own source. A task like "improve the prompt for script mode" or "add a new driver for kubectl" doesn't require anything special — the agent opens the file, edits it, saves. The same loop that processes CSV files or browses websites can rewrite the code that drives the loop.

The question isn't whether self-modification is possible. It's whether it can be made safe. And "safe" has a different meaning when the code is also serving requests.

## The constitution

clive's self-modification system starts with a classification. Every file in the project belongs to a tier, and each tier has different rules for who can change it.

```python
FILE_TIERS = [
    ("selfmod/gate.py",        "IMMUTABLE"),
    (".clive/constitution.md", "IMMUTABLE"),
    (".clive/audit/",          "IMMUTABLE"),
    ("selfmod/",               "GOVERNANCE"),
    (".env",                   "GOVERNANCE"),
    ("clive.py",               "CORE"),
    ("executor.py",            "CORE"),
    ("planner.py",             "CORE"),
    ...
]
```

**Immutable** files cannot be modified. Ever. By any role. The safety gate itself is immutable — if an agent could modify the gate, it could disable the gate, and the whole system collapses. The audit trail is immutable for the same reason. These files are the anchors.

**Governance** files (the rest of `selfmod/`, the environment config) require three approvals: a proposer who generates the change, a reviewer who evaluates the code, and an auditor who checks governance compliance. All three are LLM calls with different system prompts and, critically, different information boundaries — the reviewer sees only the diff, not the proposer's reasoning. The auditor sees only the verdict and the diff, not the reviewer's analysis.

**Core** files (the main orchestrator, the executor, the planner) require two approvals: proposer and reviewer. **Standard** files require only the proposer. **Open** files (drivers, skills, tests) need no approval.

The tier system is a gradient of caution proportional to blast radius. Changing a driver prompt can make one tool work poorly. Changing the executor can break all task execution. Changing the gate can disable all safety checks. The approval requirements scale with the risk.

## The gate

Above the tier system sits a deterministic gate — a pure regex scanner that has unconditional veto power over any proposed modification.

```python
BANNED_PATTERNS = [
    (re.compile(r"os\.system\s*\("),       "os.system() call"),
    (re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"), "subprocess with shell=True"),
    (re.compile(r"(?<!\w)eval\s*\("),      "eval() call"),
    (re.compile(r"(?<!\w)exec\s*\("),      "exec() call"),
    (re.compile(r"import\s+ctypes"),       "ctypes import"),
    (re.compile(r"importlib\.reload\s*\("), "importlib.reload()"),
    (re.compile(r"__import__\s*\("),       "dynamic __import__()"),
]
```

No `eval()`. No `exec()`. No `os.system()`. No dynamic imports. No obfuscated base64 strings. These patterns are checked against the proposed file contents after all approvals — a unanimous vote from proposer, reviewer, and auditor cannot override a banned pattern. The gate doesn't reason. It matches strings. It cannot be persuaded.

The gate file is immutable. The list of banned patterns is hardcoded. There is no configuration, no override, no escape hatch. This is the one component of the system that must be incapable of being influenced by an LLM, because the purpose of the gate is to constrain what LLMs can do.

## The problem with apply

The pipeline works: propose a change, review it, audit it, scan it through the gate, apply it. But "apply" hides the hard part.

```python
def apply_changes(files: dict[str, str]):
    for filepath, content in files.items():
        path = PROJECT_ROOT / filepath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
```

This writes files directly to the running codebase. If clive is in the middle of executing a task when `executor.py` is overwritten — Python has already loaded the module. The running process uses the old code. But the next task will import the new code. If the new code has a bug, the next task fails. If clive is in server mode with four workers, some workers run the old code and some run the new code during the transition window.

For a single-user CLI tool, this is manageable. You modify the code, you run clive again, you see if it works. For a server handling concurrent requests, it's a race condition that produces silent corruption.

The solution is atomic apply via git. Instead of writing files directly:

1. Create a branch: `selfmod/{proposal_id}`
2. Apply changes on the branch
3. Run the test suite against the branch
4. If tests pass, merge into the working branch
5. If tests fail, delete the branch — the working code is untouched

The merge is the commit point. Before the merge, nothing has changed. After the merge, the new code is on disk but no running process has loaded it. A hot-reload signal tells workers to finish their current job and restart, picking up the new code cleanly.

```
propose → review → audit → gate → branch → apply → test → merge → signal workers → restart
```

Each step can abort. The branch is the safety net — if anything goes wrong between apply and merge, `git checkout` restores the working code instantly. The snapshot mechanism that already exists in the pipeline (a git tag before apply) becomes redundant, replaced by the branch itself.

## Eval-gated evolution

Tests catch regressions in correctness. They don't catch regressions in quality. A self-modification that passes all unit tests but makes the planner 30% less effective at decomposing tasks is not a test failure — it's a capability regression that only shows up in evaluation.

clive has a four-layer eval framework. Layer 1 tests basic shell tasks. Layer 2 tests specific tools. Layer 3 tests error recovery. Layer 4 tests planning quality. Each layer has baseline scores — completion rate, turn efficiency, token cost — stored as JSON from previous runs.

The eval gate adds one rule: a self-modification that causes eval scores to regress below the baseline is rejected, even if all unit tests pass. The pipeline identifies which eval layers are affected by the changed files (modifications to `executor.py` trigger Layer 2 and 3; modifications to `planner.py` trigger Layer 4), runs those evals, and compares against the stored baseline.

This closes the loop between self-modification and measured quality. The agent can propose prompt improvements, execution optimizations, new strategies — and each proposal is evaluated not just for correctness but for whether it actually makes things better. A prompt change that seems reasonable to the reviewer but scores worse in practice gets rejected by the numbers.

## The philosophical boundary

There's a question underneath all of this machinery: how far should self-modification go?

The current answer is conservative. The immutable gate constrains the mechanism. The tier system constrains the scope. The eval gate constrains the outcome. A self-modification that passes all three is almost certainly safe and almost certainly an improvement.

But "almost certainly" is doing a lot of work. The proposer, reviewer, and auditor are all LLM instances. They're good at catching obvious problems and bad at catching subtle ones. A modification that slightly biases the planner toward certain tools, or gradually shifts the system prompt in a particular direction, or introduces a performance regression too small to trip the eval threshold — these are the failure modes that governance doesn't catch.

The mitigation is the audit trail. Every proposal, every review, every gate result, every eval comparison is logged to an append-only directory. The trail is immutable — it's in the same tier as the gate itself. A human reviewing the audit log can see the trajectory of self-modifications over time: what changed, why, what the scores were before and after. Drift that's invisible in any single modification becomes visible in the aggregate.

The deeper mitigation is the rate limit. Five modifications per session. This isn't a technical constraint — it's a philosophical one. Self-modification should be deliberate and infrequent, not a continuous optimization loop. The agent should spend most of its time doing work, not improving itself. The rate limit enforces this boundary mechanically, regardless of how compelling the next proposed improvement might seem.

## Running hot

The unsolved problem is the transition between old code and new code in a running server. The git-branch approach makes the code change atomic. The hot-reload signal makes the restart coordinated. But between the merge and the last worker restarting, the server is running mixed versions.

For most changes this is fine — a new driver prompt, a tweaked system message, an additional tool in the registry. These are additive changes that don't break the interface between components. For structural changes — a new field in the `Subtask` dataclass, a renamed function in `executor.py`, a changed signature in `llm.py` — the mixed-version window is dangerous.

The conservative answer is to drain the queue before applying structural changes. The supervisor stops accepting new jobs, waits for all workers to finish, applies the change, restarts all workers simultaneously. This creates a brief downtime window but eliminates the mixed-version problem entirely.

The aggressive answer is rolling restarts with version-aware routing — new jobs go to updated workers, old jobs finish on old workers. This is what web services do with blue-green deploys. It's also significantly more complex than a CLI tool's self-modification system should be.

The current choice is the conservative one. Drain, apply, restart. Simple, correct, and compatible with the rate limit that keeps self-modifications infrequent enough that brief pauses don't matter.

The code is at [github.com/ikangai/clive](https://github.com/ikangai/clive).
