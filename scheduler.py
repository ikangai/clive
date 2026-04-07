"""Cron scheduling for clive tasks.

Manages scheduled task entries: add, list, remove.
Tasks run via cron using `clive --quiet --json` and persist results.
"""
import json
import os
import subprocess
import time

SCHEDULE_DIR = os.path.expanduser("~/.clive/schedules")
RESULTS_DIR = os.path.expanduser("~/.clive/results")


def _ensure_dirs():
    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)


def add_schedule(task: str, cron_expr: str, name: str | None = None, notify: str = "") -> dict:
    """Add a scheduled task.

    Args:
        task: The task description for clive
        cron_expr: Cron expression (e.g., "0 * * * *" for hourly)
        name: Optional name for the schedule (auto-generated if not provided)
        notify: Notification method ("" for none, "file" for file-based)

    Returns: schedule entry dict
    """
    _ensure_dirs()

    if not name:
        name = task[:30].replace(" ", "_").replace("/", "_").lower()
        name = "".join(c for c in name if c.isalnum() or c == "_")

    entry = {
        "name": name,
        "task": task,
        "cron": cron_expr,
        "notify": notify,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "active": True,
    }

    path = os.path.join(SCHEDULE_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(entry, f, indent=2)

    # Install cron entry
    _install_cron(entry)

    return entry


def remove_schedule(name: str) -> bool:
    """Remove a scheduled task."""
    path = os.path.join(SCHEDULE_DIR, f"{name}.json")
    if os.path.exists(path):
        os.unlink(path)
        _uninstall_cron(name)
        return True
    return False


def list_schedules() -> list[dict]:
    """List all scheduled tasks."""
    _ensure_dirs()
    schedules = []
    for fname in sorted(os.listdir(SCHEDULE_DIR)):
        if fname.endswith(".json"):
            path = os.path.join(SCHEDULE_DIR, fname)
            with open(path) as f:
                schedules.append(json.load(f))
    return schedules


def get_history(name: str, limit: int = 10) -> list[dict]:
    """Get recent run history for a scheduled task."""
    _ensure_dirs()
    history = []
    results_path = os.path.join(RESULTS_DIR, name)
    if not os.path.isdir(results_path):
        return history
    for fname in sorted(os.listdir(results_path), reverse=True)[:limit]:
        if fname.endswith(".json"):
            with open(os.path.join(results_path, fname)) as f:
                history.append(json.load(f))
    return history


def _get_clive_path() -> str:
    """Find the clive.py script path."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "clive.py")


def _install_cron(entry: dict):
    """Add a cron entry for a scheduled task."""
    clive_path = _get_clive_path()
    results_dir = os.path.join(RESULTS_DIR, entry["name"])
    os.makedirs(results_dir, exist_ok=True)

    # The cron command: run clive --quiet --json, save result
    cron_cmd = (
        f'{entry["cron"]} '
        f'python3 {clive_path} --quiet --json "{entry["task"]}" '
        f'> {results_dir}/$(date +\\%Y\\%m\\%d_\\%H\\%M\\%S).json 2>&1'
    )

    # Read current crontab, add entry
    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except Exception:
        current = ""

    marker = f"# clive-schedule:{entry['name']}"
    # Remove old entry if exists
    lines = [l for l in current.splitlines() if marker not in l and entry["name"] not in l]
    lines.append(f"{cron_cmd} {marker}")

    # Install
    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to install crontab: {proc.stderr}")


def _uninstall_cron(name: str):
    """Remove a cron entry for a scheduled task."""
    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except Exception:
        return

    marker = f"# clive-schedule:{name}"
    lines = [l for l in current.splitlines() if marker not in l]

    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                   capture_output=True, text=True)
