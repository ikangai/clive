# selfmod/eval_gate.py
"""Eval gate for self-modification — blocks changes that cause eval regression."""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINES_DIR = PROJECT_ROOT / "evals" / "baselines"

# Map files to which eval layers they affect
FILE_EVAL_MAP = {
    "executor.py": ["layer2", "layer3"],
    "planner.py": ["layer2"],
    "llm.py": ["layer2", "layer3", "layer4"],
    "prompts.py": ["layer2", "layer3"],
    "models.py": ["layer2"],
    "session.py": ["layer2"],
    "completion.py": ["layer2"],
    "clive.py": ["layer2", "layer3"],
    "toolsets.py": ["layer2"],
    "remote.py": ["layer3", "layer4"],
    "agents.py": ["layer4"],
}


@dataclass
class EvalGateResult:
    passed: bool
    message: str
    baseline_score: float = 0.0
    new_score: float = 0.0
    details: dict | None = None


def identify_affected_evals(changed_files: list[str]) -> list[str]:
    """Identify which eval layers are affected by the changed files."""
    affected = set()
    for f in changed_files:
        basename = os.path.basename(f)
        if basename in FILE_EVAL_MAP:
            affected.update(FILE_EVAL_MAP[basename])
        elif basename.endswith(".py"):
            # Unknown Python file — default to layer2
            affected.add("layer2")
        # Non-Python files (docs, configs) don't trigger evals
    return sorted(affected)


def load_baseline(layer: str) -> float:
    """Load the baseline score for an eval layer."""
    baseline_file = BASELINES_DIR / f"{layer}.json"
    if not baseline_file.exists():
        log.warning("No baseline for %s, skipping comparison", layer)
        return 0.0
    try:
        data = json.loads(baseline_file.read_text())
        return data.get("completion_rate", data.get("score", 0.0))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load baseline for %s: %s", layer, e)
        return 0.0


def run_eval_layer(layer: str) -> float:
    """Run an eval layer and return the completion rate."""
    eval_dir = PROJECT_ROOT / "evals" / layer
    if not eval_dir.exists():
        log.warning("Eval directory %s does not exist", eval_dir)
        return 0.0
    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", str(eval_dir), "-v", "--tb=short", "-q"],
            capture_output=True, text=True,
            timeout=120,
            cwd=PROJECT_ROOT,
        )
        # Parse pass rate from pytest summary line (e.g. "5 passed, 2 failed")
        passed = 0
        failed = 0
        for line in result.stdout.splitlines():
            m_passed = re.search(r"(\d+)\s+passed", line)
            m_failed = re.search(r"(\d+)\s+failed", line)
            if m_passed:
                passed = int(m_passed.group(1))
            if m_failed:
                failed = int(m_failed.group(1))
        total = passed + failed
        if total > 0:
            return passed / total
        # Fallback: use exit code (0 = all passed)
        return 1.0 if result.returncode == 0 else 0.0
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Eval %s failed to run: %s", layer, e)
        return 0.0


def check_eval_gate(changed_files: list[str], dry_run: bool = False) -> EvalGateResult:
    """Check if changes pass the eval gate.

    Args:
        changed_files: list of file paths being modified
        dry_run: if True, skip actual eval runs and always pass

    Returns:
        EvalGateResult indicating whether changes are safe
    """
    if dry_run:
        return EvalGateResult(passed=True, message="Dry run — eval gate skipped")

    affected = identify_affected_evals(changed_files)
    if not affected:
        return EvalGateResult(passed=True, message="No evals affected")

    log.info("Running eval gate for layers: %s", affected)

    worst_regression = 0.0
    details = {}

    for layer in affected:
        baseline = load_baseline(layer)
        new_score = run_eval_layer(layer)
        regression = baseline - new_score

        details[layer] = {
            "baseline": baseline,
            "new_score": new_score,
            "regression": regression,
        }

        if regression > worst_regression:
            worst_regression = regression

    # Allow up to 5% regression (noise margin)
    if worst_regression > 0.05:
        return EvalGateResult(
            passed=False,
            message=f"Eval regression detected: {worst_regression:.1%}",
            baseline_score=max(d["baseline"] for d in details.values()) if details else 0,
            new_score=min(d["new_score"] for d in details.values()) if details else 0,
            details=details,
        )

    return EvalGateResult(
        passed=True,
        message=f"Eval gate passed ({len(affected)} layers checked)",
        details=details,
    )
