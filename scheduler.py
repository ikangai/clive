"""Cron scheduling for clive tasks.

Manages scheduled task entries: add, list, remove, pause, run-now.
Tasks run via a wrapper script that sources .env, sets PATH, captures
structured results, and handles failure notification.

Results are structured JSON: {timestamp, status, result, duration, tokens, error}
Retention: configurable, default 30 days.
"""
import json
import os
import subprocess
import time

import re

SCHEDULE_DIR = os.path.expanduser("~/.clive/schedules")
RESULTS_DIR = os.path.expanduser("~/.clive/results")
DEFAULT_RETENTION_DAYS = 30

# Cron expression validation: 5 fields (min hour dom month dow)
_CRON_FIELD = r'(\*(?:/\d+)?|[\d,\-/\*]+)'
_CRON_RE = re.compile(rf'^{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}$')


def validate_cron(expr: str) -> bool:
    """Validate a cron expression (5 fields: min hour dom month dow)."""
    return bool(_CRON_RE.match(expr.strip()))


def _ensure_dirs():
    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)


# ─── Schedule Management ─────────────────────────────────────────────────────

def add_schedule(
    task: str,
    cron_expr: str,
    name: str | None = None,
    notify: str = "",
    toolset: str = "minimal",
) -> dict:
    """Add a scheduled task.

    Args:
        task: Task description for clive
        cron_expr: Cron expression (e.g., "0 * * * *")
        name: Schedule name (auto-generated if not provided)
        notify: "email:addr" or "file:/path" or "" for none
        toolset: Toolset to use (default: minimal)
    """
    _ensure_dirs()

    if not validate_cron(cron_expr):
        raise ValueError(f"Invalid cron expression: '{cron_expr}'. Expected 5 fields: min hour dom month dow")

    if not name:
        name = _auto_name(task)

    # Detect update: if schedule with this name already exists, note the change
    existing_path = os.path.join(SCHEDULE_DIR, f"{name}.json")
    if os.path.exists(existing_path):
        with open(existing_path) as f:
            old = json.load(f)
        if old.get("cron") != cron_expr:
            _updated_from = old.get("cron")
        else:
            _updated_from = None
    else:
        _updated_from = None

    entry = {
        "name": name,
        "task": task,
        "cron": cron_expr,
        "notify": notify,
        "toolset": toolset,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "active": True,
    }

    path = os.path.join(SCHEDULE_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(entry, f, indent=2)

    # Write the wrapper script and install cron
    _write_wrapper(entry)
    _install_cron(entry)

    if _updated_from:
        entry["_updated_from"] = _updated_from

    return entry


def remove_schedule(name: str) -> bool:
    """Remove a scheduled task and its cron entry."""
    path = os.path.join(SCHEDULE_DIR, f"{name}.json")
    if os.path.exists(path):
        os.unlink(path)
        _uninstall_cron(name)
        # Remove wrapper script
        wrapper = os.path.join(SCHEDULE_DIR, f"{name}.sh")
        if os.path.exists(wrapper):
            os.unlink(wrapper)
        return True
    return False


def pause_schedule(name: str) -> bool:
    """Pause a schedule (keep definition, remove cron entry)."""
    path = os.path.join(SCHEDULE_DIR, f"{name}.json")
    if not os.path.exists(path):
        return False
    with open(path) as f:
        entry = json.load(f)
    entry["active"] = False
    with open(path, "w") as f:
        json.dump(entry, f, indent=2)
    _uninstall_cron(name)
    return True


def resume_schedule(name: str) -> bool:
    """Resume a paused schedule."""
    path = os.path.join(SCHEDULE_DIR, f"{name}.json")
    if not os.path.exists(path):
        return False
    with open(path) as f:
        entry = json.load(f)
    entry["active"] = True
    with open(path, "w") as f:
        json.dump(entry, f, indent=2)
    _install_cron(entry)
    return True


def list_schedules() -> list[dict]:
    """List all scheduled tasks with last run status."""
    _ensure_dirs()
    schedules = []
    for fname in sorted(os.listdir(SCHEDULE_DIR)):
        if fname.endswith(".json"):
            path = os.path.join(SCHEDULE_DIR, fname)
            with open(path) as f:
                entry = json.load(f)
            # Annotate with health stats
            health = get_health(entry["name"])
            entry["health"] = health
            if health["runs"] > 0:
                entry["last_run"] = get_history(entry["name"], limit=1)[0].get("timestamp", "?")
                entry["last_status"] = health["last_status"]
            schedules.append(entry)
    return schedules


# ─── Run Now ──────────────────────────────────────────────────────────────────

def run_now(name: str) -> dict:
    """Manually trigger a scheduled task immediately. Returns the result."""
    path = os.path.join(SCHEDULE_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Schedule not found: {name}")

    with open(path) as f:
        entry = json.load(f)

    wrapper = os.path.join(SCHEDULE_DIR, f"{name}.sh")
    if not os.path.exists(wrapper):
        _write_wrapper(entry)

    result = subprocess.run(
        ["bash", wrapper],
        capture_output=True, text=True, timeout=300,
    )

    # Read the most recent result file
    history = get_history(name, limit=1)
    return history[0] if history else {"status": "unknown", "output": result.stdout[:500]}


# ─── History ──────────────────────────────────────────────────────────────────

def get_history(name: str, limit: int = 10) -> list[dict]:
    """Get recent run history for a scheduled task."""
    _ensure_dirs()
    history = []
    results_path = os.path.join(RESULTS_DIR, name)
    if not os.path.isdir(results_path):
        return history

    for fname in sorted(os.listdir(results_path), reverse=True)[:limit]:
        if fname.endswith(".json"):
            fpath = os.path.join(results_path, fname)
            try:
                with open(fpath) as f:
                    entry = json.load(f)
                history.append(entry)
            except (json.JSONDecodeError, OSError):
                # Malformed result file — create a minimal entry
                history.append({
                    "timestamp": fname.replace(".json", ""),
                    "status": "parse_error",
                    "error": "Result file is not valid JSON",
                })
    return history


def get_health(name: str, window: int = 10) -> dict:
    """Get health stats for a schedule over the last N runs."""
    history = get_history(name, limit=window)
    if not history:
        return {"runs": 0, "success_rate": 0, "avg_duration": 0, "streak": "none"}

    successes = sum(1 for h in history if h.get("status") == "success")
    durations = [h.get("duration_seconds", 0) for h in history if isinstance(h.get("duration_seconds"), (int, float))]
    avg_dur = sum(durations) / len(durations) if durations else 0

    # Failure streak: count consecutive failures from most recent
    streak = 0
    for h in history:
        if h.get("status") == "success":
            break
        streak += 1

    return {
        "runs": len(history),
        "success_rate": round(successes / len(history), 2) if history else 0,
        "avg_duration": round(avg_dur, 1),
        "failure_streak": streak,
        "last_status": history[0].get("status", "?") if history else "?",
    }


def cleanup_history(name: str, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Remove result files older than retention_days. Returns count removed."""
    results_path = os.path.join(RESULTS_DIR, name)
    if not os.path.isdir(results_path):
        return 0

    cutoff = time.time() - (retention_days * 86400)
    removed = 0
    for fname in os.listdir(results_path):
        fpath = os.path.join(results_path, fname)
        if os.path.getmtime(fpath) < cutoff:
            os.unlink(fpath)
            removed += 1
    return removed


# ─── Wrapper Script ───────────────────────────────────────────────────────────

def _write_wrapper(entry: dict):
    """Write a wrapper script that handles environment, execution, and result capture."""
    clive_path = _get_clive_path()
    clive_dir = os.path.dirname(clive_path)
    results_dir = os.path.join(RESULTS_DIR, entry["name"])
    os.makedirs(results_dir, exist_ok=True)

    import shlex
    notify_cmd = ""
    if entry.get("notify", "").startswith("email:"):
        addr = shlex.quote(entry["notify"].split(":", 1)[1])
        notify_cmd = f'  echo "$_result" | mail -s "clive [{entry["name"]}] FAILED" {addr}'
    elif entry.get("notify", "").startswith("file:"):
        fpath = shlex.quote(entry["notify"].split(":", 1)[1])
        notify_cmd = f'  echo "[$(date -Iseconds)] {entry["name"]} FAILED: $_result" >> {fpath}'

    toolset_flag = f'-t {entry.get("toolset", "minimal")}' if entry.get("toolset") else ""

    lock_file = os.path.join(SCHEDULE_DIR, f"{entry['name']}.lock")

    wrapper = f"""#!/bin/bash
# Auto-generated by clive scheduler for: {entry["name"]}
# Do not edit — regenerated on schedule changes

set -o pipefail

# Concurrency guard: skip if previous run still active
exec 200>{lock_file}
if ! flock -n 200; then
  echo "Skipping: previous run of {entry['name']} still active" >&2
  exit 0
fi

# Source environment (API keys, provider config)
cd {clive_dir}
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# Ensure PATH includes common locations
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export TERM=xterm-256color

# Run clive with structured output
_start=$(date +%s)
_result=$(python3 {clive_path} --quiet --json {toolset_flag} "{entry["task"]}" 2>/dev/null)
_exit=$?
_end=$(date +%s)
_duration=$((_end - _start))

# Write structured result
_timestamp=$(date +%Y%m%d_%H%M%S)
_result_file="{results_dir}/$_timestamp.json"

python3 -c "
import json, sys
result = sys.argv[1]
try:
    parsed = json.loads(result)
except Exception:
    parsed = result
json.dump({{
    'timestamp': '$_timestamp',
    'status': 'success' if $_exit == 0 else 'failed',
    'exit_code': $_exit,
    'duration_seconds': $_duration,
    'result': parsed,
    'schedule': '{entry["name"]}',
}}, open('$_result_file', 'w'), indent=2)
" "$_result"

# Notify on failure
if [ $_exit -ne 0 ]; then
{notify_cmd if notify_cmd else '  true  # no notification configured'}
fi

# Cleanup old results (keep last 30 days)
find {results_dir} -name "*.json" -mtime +30 -delete 2>/dev/null
"""

    wrapper_path = os.path.join(SCHEDULE_DIR, f"{entry['name']}.sh")
    with open(wrapper_path, "w") as f:
        f.write(wrapper)
    os.chmod(wrapper_path, 0o755)


def _get_clive_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "clive.py")


def _auto_name(task: str) -> str:
    name = task[:30].replace(" ", "_").replace("/", "_").lower()
    return "".join(c for c in name if c.isalnum() or c == "_")


# ─── Crontab Management ──────────────────────────────────────────────────────

def _install_cron(entry: dict):
    """Install cron entry pointing to the wrapper script."""
    wrapper_path = os.path.join(SCHEDULE_DIR, f"{entry['name']}.sh")

    cron_line = f'{entry["cron"]} {wrapper_path}'
    marker = f"# clive-schedule:{entry['name']}"

    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except Exception:
        current = ""

    lines = [l for l in current.splitlines() if marker not in l]
    lines.append(f"{cron_line} {marker}")

    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to install crontab: {proc.stderr}")


def _uninstall_cron(name: str):
    """Remove cron entry for a schedule."""
    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except Exception:
        return

    marker = f"# clive-schedule:{name}"
    lines = [l for l in current.splitlines() if marker not in l]

    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                   capture_output=True, text=True)
