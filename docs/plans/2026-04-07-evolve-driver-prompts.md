# Evolutionary Driver Prompt Optimization

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `clive --evolve shell` command that generates driver prompt variants, evaluates each against the eval suite, and keeps the best-scoring variant — enabling automated prompt evolution.

**Architecture:** `evolve.py` orchestrates a generate-evaluate-select loop. The LLM produces N variant driver prompts informed by current eval results. Each variant is written to a temp file and passed to the eval runner via a driver override mechanism. A fitness function scores each variant on pass rate, turn efficiency, and token efficiency. The best variant replaces the current driver if it beats baseline. Lineage is tracked in `drivers/history/`.

**Tech Stack:** Python 3, existing eval harness (`evals/harness/`), existing LLM client (`llm.py`)

---

### Task 1: Wire driver override through load_driver

The eval runner sets `CLIVE_EVAL_DRIVER_OVERRIDE` env var but `load_driver()` never reads it. Wire it up so the override actually takes effect.

**Files:**
- Modify: `prompts.py:13-24`
- Create: `tests/test_driver_override.py`

**Step 1: Write the failing test**

Create `tests/test_driver_override.py`:

```python
"""Tests for driver prompt override."""
import os
from prompts import load_driver


def test_driver_override_via_env(tmp_path, monkeypatch):
    """CLIVE_EVAL_DRIVER_OVERRIDE should override any driver."""
    override_file = tmp_path / "custom_shell.md"
    override_file.write_text("CUSTOM OVERRIDE DRIVER")
    monkeypatch.setenv("CLIVE_EVAL_DRIVER_OVERRIDE", str(override_file))
    result = load_driver("shell")
    assert result == "CUSTOM OVERRIDE DRIVER"


def test_driver_override_not_set():
    """Without env var, load_driver behaves normally."""
    os.environ.pop("CLIVE_EVAL_DRIVER_OVERRIDE", None)
    result = load_driver("shell")
    assert "Shell Driver" in result or "shell" in result.lower()
    assert len(result) > 50


def test_driver_override_missing_file(monkeypatch):
    """If override file doesn't exist, fall back to normal behavior."""
    monkeypatch.setenv("CLIVE_EVAL_DRIVER_OVERRIDE", "/nonexistent/driver.md")
    result = load_driver("shell")
    assert len(result) > 50  # should fall back to real driver
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_driver_override.py -v`
Expected: `test_driver_override_via_env` FAILS (override not implemented)

**Step 3: Update load_driver in prompts.py**

Replace the `load_driver` function:

```python
def load_driver(app_type: str, drivers_dir: str | None = None) -> str:
    """Load a driver prompt for the given app_type.

    Auto-discovers drivers from the drivers/ directory by matching
    {app_type}.md. Falls back to DEFAULT_DRIVER if no file found.

    If CLIVE_EVAL_DRIVER_OVERRIDE env var is set to a file path,
    that file is used instead (for eval/evolution overrides).
    """
    override = os.environ.get("CLIVE_EVAL_DRIVER_OVERRIDE")
    if override and os.path.exists(override):
        with open(override, "r") as f:
            return f.read().strip()

    base = drivers_dir or _DRIVERS_DIR
    path = os.path.join(base, f"{app_type}.md")
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    return DEFAULT_DRIVER
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_driver_override.py -v`
Expected: 3 PASSED

**Step 5: Run all tests to check for regressions**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/ -v`
Expected: All PASSED

**Step 6: Commit**

```bash
git add prompts.py tests/test_driver_override.py
git commit -m "feat: wire driver override env var through load_driver"
```

---

### Task 2: Fitness scoring function

Create the fitness scoring module that takes an `EvalReport` and returns a composite score.

**Files:**
- Create: `evolve_fitness.py`
- Create: `tests/test_evolve_fitness.py`

**Step 1: Write the failing test**

Create `tests/test_evolve_fitness.py`:

```python
"""Tests for evolution fitness scoring."""
from evolve_fitness import fitness_score
from evals.harness.metrics import EvalResult, EvalReport


def _make_result(passed=True, turns=3, min_turns=2, tokens=3000):
    return EvalResult(
        task_id="test", layer=2, tool="shell", passed=passed,
        turns_used=turns, min_turns=min_turns,
        prompt_tokens=tokens // 2, completion_tokens=tokens // 2,
        elapsed_seconds=10.0, detail="test",
    )


def test_perfect_score():
    results = [_make_result(passed=True, turns=2, min_turns=2, tokens=1000)]
    report = EvalReport(results)
    score = fitness_score(report)
    assert score > 0.9


def test_failed_task_lowers_score():
    results = [
        _make_result(passed=True, turns=2, min_turns=2, tokens=1000),
        _make_result(passed=False, turns=5, min_turns=2, tokens=5000),
    ]
    report = EvalReport(results)
    score = fitness_score(report)
    assert score < 0.7


def test_more_turns_lowers_score():
    efficient = [_make_result(turns=2, min_turns=2, tokens=2000)]
    inefficient = [_make_result(turns=8, min_turns=2, tokens=2000)]
    score_eff = fitness_score(EvalReport(efficient))
    score_ineff = fitness_score(EvalReport(inefficient))
    assert score_eff > score_ineff


def test_more_tokens_lowers_score():
    cheap = [_make_result(turns=3, min_turns=2, tokens=1000)]
    expensive = [_make_result(turns=3, min_turns=2, tokens=20000)]
    score_cheap = fitness_score(EvalReport(cheap))
    score_expensive = fitness_score(EvalReport(expensive))
    assert score_cheap > score_expensive


def test_zero_tasks():
    report = EvalReport([])
    score = fitness_score(report)
    assert score == 0.0
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_evolve_fitness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evolve_fitness'`

**Step 3: Implement evolve_fitness.py**

Create `evolve_fitness.py`:

```python
"""Fitness scoring for driver prompt evolution.

Composite score from three metrics:
  - pass_rate (weight 0.5): tasks passed / total
  - turn_efficiency (weight 0.3): min_turns / actual_turns, averaged
  - token_efficiency (weight 0.2): inverse of normalized token usage

Pass rate has a hard floor: if pass_rate < baseline, fitness is 0.
"""
from evals.harness.metrics import EvalReport

# Weights for composite score
W_PASS = 0.5
W_TURN = 0.3
W_TOKEN = 0.2

# Token budget per task (used to normalize token_efficiency)
TOKEN_BUDGET_PER_TASK = 10_000


def fitness_score(report: EvalReport, baseline_pass_rate: float = 0.0) -> float:
    """Compute composite fitness score (0.0 - 1.0).

    Returns 0.0 if pass_rate drops below baseline_pass_rate.
    """
    if report.total_tasks == 0:
        return 0.0

    pass_rate = report.completion_rate

    # Hard constraint: never trade correctness for speed
    if pass_rate < baseline_pass_rate:
        return 0.0

    turn_efficiency = report.avg_turn_efficiency

    # Token efficiency: 1.0 when using 0 tokens, 0.0 at budget, negative above
    avg_tokens = report.total_tokens / report.total_tasks
    token_efficiency = max(0.0, 1.0 - avg_tokens / TOKEN_BUDGET_PER_TASK)

    return (W_PASS * pass_rate) + (W_TURN * turn_efficiency) + (W_TOKEN * token_efficiency)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_evolve_fitness.py -v`
Expected: 5 PASSED

**Step 5: Commit**

```bash
git add evolve_fitness.py tests/test_evolve_fitness.py
git commit -m "feat: add fitness scoring for driver prompt evolution"
```

---

### Task 3: Variant generation (mutation)

Create the mutation module that takes a current driver prompt + eval results and produces N variants.

**Files:**
- Create: `evolve_mutate.py`
- Create: `tests/test_evolve_mutate.py`

**Step 1: Write the failing test**

Create `tests/test_evolve_mutate.py`:

```python
"""Tests for driver prompt mutation."""
from evolve_mutate import build_mutation_prompt, STRATEGIES


def test_strategies_exist():
    assert len(STRATEGIES) >= 3
    for s in STRATEGIES:
        assert "name" in s
        assert "goal" in s


def test_build_mutation_prompt_contains_driver():
    prompt = build_mutation_prompt(
        current_driver="# Shell Driver\nCOMMAND EXECUTION: one per turn",
        eval_summary="5/5 passed, avg 4 turns, 5000 tokens/task",
        strategy=STRATEGIES[0],
    )
    assert "Shell Driver" in prompt
    assert "5/5 passed" in prompt
    assert STRATEGIES[0]["goal"] in prompt


def test_build_mutation_prompt_has_constraints():
    prompt = build_mutation_prompt(
        current_driver="# Test\nshort",
        eval_summary="3/5 passed",
        strategy=STRATEGIES[0],
    )
    assert "80 lines" in prompt or "compact" in prompt.lower()
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_evolve_mutate.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Implement evolve_mutate.py**

Create `evolve_mutate.py`:

```python
"""Mutation strategies for driver prompt evolution.

Each strategy targets a different optimization goal.
The LLM sees the current driver + eval results and produces an improved version.
"""
import os
import tempfile

from llm import get_client, chat

STRATEGIES = [
    {
        "name": "token_optimizer",
        "goal": "Minimize total token usage across all tasks. Make instructions more concise. Remove redundant examples. Use terse, high-signal phrasing.",
    },
    {
        "name": "turn_optimizer",
        "goal": "Minimize the number of turns needed to complete tasks. Help the agent get things right on the first try. Add patterns for common operations so the agent doesn't need to explore.",
    },
    {
        "name": "robustness_optimizer",
        "goal": "Minimize failures and repair loops. Add error prevention patterns. Warn about common pitfalls more prominently. Improve script-mode compatibility.",
    },
]


def build_mutation_prompt(
    current_driver: str,
    eval_summary: str,
    strategy: dict,
) -> str:
    return f"""You are optimizing a driver prompt for a terminal agent.

The agent reads the terminal screen and types commands. The driver prompt is a compact reference card that gives the agent tool-specific knowledge. Better driver prompts = fewer turns, fewer tokens, fewer failures.

Current driver prompt:
---
{current_driver}
---

Last eval results:
{eval_summary}

Optimization goal: {strategy["goal"]}

Constraints:
- Must remain a compact reference card (under 80 lines)
- Keep the same markdown structure (# heading, SECTION: content)
- Do not remove information categories, only restructure or clarify
- Do not add conversational text or explanations — terse reference format only

Write the improved driver prompt. Output ONLY the driver prompt content, no explanation."""


def generate_variants(
    driver_path: str,
    eval_summary: str,
    num_variants: int = 3,
) -> list[str]:
    """Generate N variant driver prompts. Returns list of temp file paths."""
    with open(driver_path, "r") as f:
        current_driver = f.read().strip()

    client = get_client()
    variants = []

    for i in range(num_variants):
        strategy = STRATEGIES[i % len(STRATEGIES)]

        prompt = build_mutation_prompt(current_driver, eval_summary, strategy)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Generate the improved driver prompt."},
        ]

        reply, _, _ = chat(client, messages, max_tokens=4096)

        # Strip markdown fences if present
        content = reply.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = lines[1:]  # remove opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines)

        # Write to temp file
        fd, path = tempfile.mkstemp(suffix=".md", prefix=f"driver_variant_{i}_")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        variants.append(path)

    return variants


def format_eval_summary(report_dict: dict) -> str:
    """Format an eval report dict into a summary string for the mutation prompt."""
    lines = []
    lines.append(f"{report_dict['passed']}/{report_dict['total_tasks']} passed "
                 f"({report_dict['completion_rate']:.0%})")
    lines.append(f"Turn efficiency: {report_dict['avg_turn_efficiency']:.0%}")
    lines.append(f"Total tokens: {report_dict['total_tokens']:,}")
    lines.append("")
    for r in report_dict.get("results", []):
        status = "PASS" if r["passed"] else "FAIL"
        lines.append(f"  [{status}] {r['task_id']}: {r['turns']} turns, {r['tokens']} tokens")
    return "\n".join(lines)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/test_evolve_mutate.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
git add evolve_mutate.py tests/test_evolve_mutate.py
git commit -m "feat: add mutation strategies for driver prompt evolution"
```

---

### Task 4: Evolution loop and CLI

Create `evolve.py` — the main entry point that orchestrates generate → evaluate → select → save.

**Files:**
- Create: `evolve.py`
- Modify: `clive.py` (add `--evolve` argument)

**Step 1: Create evolve.py**

Create `evolve.py`:

```python
#!/usr/bin/env python3
"""Evolutionary driver prompt optimization.

Usage:
    python3 evolve.py shell
    python3 evolve.py shell --variants 5 --generations 3
    python3 evolve.py all --dry-run
"""
import argparse
import json
import os
import shutil
import sys
import time

from dotenv import load_dotenv
load_dotenv()

from evals.harness.run_eval import load_tasks, run_single_task
from evals.harness.metrics import EvalReport
from evolve_fitness import fitness_score
from evolve_mutate import generate_variants, format_eval_summary


DRIVERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drivers")
HISTORY_DIR = os.path.join(DRIVERS_DIR, "history")


def _driver_path(driver_name: str) -> str:
    return os.path.join(DRIVERS_DIR, f"{driver_name}.md")


def _eval_tool_for_driver(driver_name: str) -> str | None:
    """Map driver name to eval tool suite."""
    mapping = {
        "shell": "shell",
        "browser": "lynx",
    }
    return mapping.get(driver_name)


def run_evals_with_driver(driver_file: str, driver_name: str) -> tuple[EvalReport, dict]:
    """Run all Layer 2 evals for a driver, return report and dict."""
    os.environ["CLIVE_EVAL_DRIVER_OVERRIDE"] = driver_file

    tool = _eval_tool_for_driver(driver_name)
    tasks = load_tasks(layer=2, tool=tool)
    # Also include script mode tasks if shell driver
    if driver_name == "shell":
        tasks.extend(load_tasks(layer=2, tool="shell_script"))

    results = []
    for task_def in tasks:
        result = run_single_task(task_def)
        results.append(result)

    if "CLIVE_EVAL_DRIVER_OVERRIDE" in os.environ:
        del os.environ["CLIVE_EVAL_DRIVER_OVERRIDE"]

    report = EvalReport(results)
    return report, report.to_dict()


def save_lineage(driver_name: str, generation: int, score: float,
                 driver_file: str, report_dict: dict):
    """Save a generation's best variant and eval results to history."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    base = f"{driver_name}_gen{generation:03d}_{score:.3f}"

    shutil.copy2(driver_file, os.path.join(HISTORY_DIR, f"{base}.md"))
    with open(os.path.join(HISTORY_DIR, f"{base}.json"), "w") as f:
        json.dump(report_dict, f, indent=2)


def evolve_driver(
    driver_name: str,
    num_variants: int = 3,
    num_generations: int = 1,
    dry_run: bool = False,
) -> dict:
    """Run the evolution loop for a single driver.

    Returns summary dict with generations, scores, and whether an improvement was found.
    """
    driver_file = _driver_path(driver_name)
    if not os.path.exists(driver_file):
        print(f"Driver not found: {driver_file}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"EVOLVING: {driver_name} ({num_variants} variants x {num_generations} generations)", file=sys.stderr)
    print(f"{'=' * 60}\n", file=sys.stderr)

    # Baseline: evaluate current driver
    print(f"Baseline eval...", file=sys.stderr)
    baseline_report, baseline_dict = run_evals_with_driver(driver_file, driver_name)
    baseline_score = fitness_score(baseline_report)
    baseline_pass_rate = baseline_report.completion_rate
    print(f"  Baseline score: {baseline_score:.3f} "
          f"({baseline_report.passed_tasks}/{baseline_report.total_tasks} passed, "
          f"{baseline_report.avg_turn_efficiency:.0%} turn eff, "
          f"{baseline_report.total_tokens:,} tokens)\n", file=sys.stderr)

    current_best_file = driver_file
    current_best_score = baseline_score
    current_best_dict = baseline_dict
    eval_summary = format_eval_summary(baseline_dict)

    generations = []

    for gen in range(1, num_generations + 1):
        print(f"Generation {gen}/{num_generations}:", file=sys.stderr)

        # Generate variants
        print(f"  Generating {num_variants} variants...", file=sys.stderr)
        variant_files = generate_variants(current_best_file, eval_summary, num_variants)

        gen_results = []
        gen_best_score = current_best_score
        gen_best_file = None
        gen_best_dict = None

        for i, vf in enumerate(variant_files):
            print(f"  Evaluating variant {i+1}/{num_variants}...", file=sys.stderr)
            report, report_dict = run_evals_with_driver(vf, driver_name)
            score = fitness_score(report, baseline_pass_rate=baseline_pass_rate)

            status = "+" if score > current_best_score else "-"
            print(f"    [{status}] score={score:.3f} "
                  f"({report.passed_tasks}/{report.total_tasks} passed, "
                  f"{report.avg_turn_efficiency:.0%} turn eff, "
                  f"{report.total_tokens:,} tokens)", file=sys.stderr)

            gen_results.append({
                "variant": i,
                "score": round(score, 3),
                "passed": report.passed_tasks,
                "total": report.total_tasks,
                "turn_efficiency": round(report.avg_turn_efficiency, 3),
                "tokens": report.total_tokens,
            })

            if score > gen_best_score:
                gen_best_score = score
                gen_best_file = vf
                gen_best_dict = report_dict

        # Select best of this generation
        improved = gen_best_score > current_best_score
        if improved and gen_best_file:
            current_best_score = gen_best_score
            current_best_file = gen_best_file
            current_best_dict = gen_best_dict
            eval_summary = format_eval_summary(gen_best_dict)
            save_lineage(driver_name, gen, gen_best_score, gen_best_file, gen_best_dict)
            print(f"  -> Improved: {gen_best_score:.3f}\n", file=sys.stderr)
        else:
            print(f"  -> No improvement (best: {gen_best_score:.3f})\n", file=sys.stderr)

        generations.append({
            "generation": gen,
            "best_score": round(gen_best_score, 3),
            "improved": improved,
            "variants": gen_results,
        })

        # Cleanup temp files
        for vf in variant_files:
            if vf != current_best_file:
                try:
                    os.unlink(vf)
                except OSError:
                    pass

    # Apply best if improved
    applied = False
    if current_best_score > baseline_score and not dry_run:
        shutil.copy2(current_best_file, driver_file)
        applied = True
        print(f"APPLIED: {driver_name}.md updated (score {baseline_score:.3f} -> {current_best_score:.3f})", file=sys.stderr)
    elif current_best_score > baseline_score:
        print(f"DRY RUN: would update {driver_name}.md (score {baseline_score:.3f} -> {current_best_score:.3f})", file=sys.stderr)
    else:
        print(f"NO CHANGE: no variant beat baseline ({baseline_score:.3f})", file=sys.stderr)

    # Cleanup remaining temp files
    if current_best_file != driver_file:
        try:
            os.unlink(current_best_file)
        except OSError:
            pass

    summary = {
        "driver": driver_name,
        "baseline_score": round(baseline_score, 3),
        "final_score": round(current_best_score, 3),
        "improved": current_best_score > baseline_score,
        "applied": applied,
        "generations": generations,
    }

    print(f"\n{'=' * 60}", file=sys.stderr)
    print(json.dumps(summary, indent=2))
    print(f"{'=' * 60}\n", file=sys.stderr)

    return summary


def main():
    parser = argparse.ArgumentParser(description="Evolve clive driver prompts")
    parser.add_argument("driver", help="Driver to evolve (shell, browser, or all)")
    parser.add_argument("--variants", type=int, default=3, help="Variants per generation (default: 3)")
    parser.add_argument("--generations", type=int, default=1, help="Number of generations (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate but don't apply changes")
    args = parser.parse_args()

    if args.driver == "all":
        drivers = ["shell", "browser"]
    else:
        drivers = [args.driver]

    for driver in drivers:
        evolve_driver(
            driver_name=driver,
            num_variants=args.variants,
            num_generations=args.generations,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
```

**Step 2: Add --evolve flag to clive.py**

In `clive.py`, after the `--safe-mode` argument in the argparse section, add:

```python
    parser.add_argument(
        "--evolve",
        metavar="DRIVER",
        help="Evolve a driver prompt (shell, browser, all)",
    )
```

And before the `run()` call at the end of `__main__`, add:

```python
    if args.evolve:
        from evolve import evolve_driver
        evolve_driver(args.evolve)
        sys.exit(0)
```

**Step 3: Verify it loads without errors**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 evolve.py --help`
Expected: Help text showing usage

**Step 4: Commit**

```bash
git add evolve.py clive.py
git commit -m "feat: add evolutionary driver prompt optimization (evolve.py)"
```

---

### Task 5: Smoke test — evolve shell driver

Run the evolution loop for real on the shell driver. This verifies the full pipeline: baseline eval → variant generation → variant eval → selection → lineage tracking.

**Files:** None (verification step)

**Step 1: Run a single-generation dry run**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 evolve.py shell --dry-run 2>&1`

Expected: Output showing baseline eval, 3 variants evaluated, best selected, "DRY RUN" message. JSON summary on stdout.

**Step 2: If it crashes, debug and fix**

Common issues:
- Driver override env var not cleaned up between variants → check cleanup in `run_evals_with_driver`
- Temp file paths not valid → check `tempfile.mkstemp` usage
- Eval tasks not found → check `load_tasks` for shell and shell_script

**Step 3: Run for real (1 generation, 3 variants)**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 evolve.py shell 2>&1`

Expected: If a variant improves, `drivers/shell.md` is updated and `drivers/history/` contains the lineage. If no variant improves, shell.md is unchanged.

**Step 4: Verify lineage was saved**

Run: `ls -la drivers/history/`
Expected: `shell_gen001_*.md` and `shell_gen001_*.json` files (if a variant improved)

**Step 5: Run all tests to confirm nothing broke**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -m pytest tests/ -v`
Expected: All PASSED

**Step 6: Commit results**

```bash
git add drivers/ evolve.py evolve_fitness.py evolve_mutate.py
git commit -m "feat: first evolutionary run — shell driver optimized"
```

---

## Summary

After completing all 5 tasks:

| Component | File | Purpose |
|---|---|---|
| Driver override | `prompts.py` | `load_driver` respects `CLIVE_EVAL_DRIVER_OVERRIDE` |
| Fitness scoring | `evolve_fitness.py` | Composite score: pass rate + turn efficiency + token efficiency |
| Mutation | `evolve_mutate.py` | 3 strategies: token, turn, robustness optimization |
| Evolution loop | `evolve.py` | Generate → evaluate → select → save lineage |
| CLI integration | `clive.py` | `--evolve` flag |

**Usage:**
```bash
python3 evolve.py shell                              # 1 gen, 3 variants
python3 evolve.py shell --variants 5 --generations 3  # deeper search
python3 evolve.py all --dry-run                       # evaluate without applying
clive --evolve shell                                  # via main CLI
```

**What evolves:** driver prompts only (plain text, no code risk).
**Selection pressure:** the eval suite (15 tasks).
**Constraint:** pass rate can never drop below baseline.
**Lineage:** `drivers/history/{driver}_gen{N}_{score}.md` + `.json`.
