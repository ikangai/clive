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
    """Run Layer 2 + Layer 3 evals for a driver, return report and dict."""
    os.environ["CLIVE_EVAL_DRIVER_OVERRIDE"] = driver_file

    tool = _eval_tool_for_driver(driver_name)
    tasks = load_tasks(layer=2, tool=tool)
    # Also include script mode tasks if shell driver
    if driver_name == "shell":
        tasks.extend(load_tasks(layer=2, tool="shell_script"))
        # Include Layer 3 for harder selection pressure
        for suite in ["script_correctness", "script_robustness", "debug_loop"]:
            l3 = load_tasks(layer=3, tool=suite)
            if l3:
                tasks.extend(l3)

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

            cost = report.estimated_cost()
            status = "+" if score > current_best_score else "-"
            cost_str = f", ${cost:.4f}" if cost > 0 else ""
            print(f"    [{status}] score={score:.3f} "
                  f"({report.passed_tasks}/{report.total_tasks} passed, "
                  f"{report.avg_turn_efficiency:.0%} turn eff, "
                  f"{report.total_tokens:,} tokens{cost_str})", file=sys.stderr)

            gen_results.append({
                "variant": i,
                "score": round(score, 3),
                "passed": report.passed_tasks,
                "total": report.total_tasks,
                "turn_efficiency": round(report.avg_turn_efficiency, 3),
                "tokens": report.total_tokens,
                "cost_usd": round(cost, 4),
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
