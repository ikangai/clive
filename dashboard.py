"""Dashboard snapshot for clive instances.

Reads the instance registry, prunes dead PIDs, and prints a table
similar to `docker ps`. Designed to be called from `clive --dashboard`
or from the TUI `/dashboard` command.
"""
import time
from pathlib import Path

from registry import list_instances, DEFAULT_REGISTRY_DIR


def _format_uptime(started_at: float) -> str:
    elapsed = time.time() - started_at
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    return f"{hours}h {minutes:02d}m"


def render_snapshot(registry_dir: Path | None = None) -> None:
    instances = list_instances(registry_dir=registry_dir)

    if not instances:
        print("No instances running.")
        return

    # Header
    print()
    print(" CLIVE INSTANCES")
    print(" " + "─" * 55)
    print(f"  {'NAME':<14}{'PID':<8}{'TOOLSET':<17}{'UPTIME':<10}")

    for inst in instances:
        name = inst.get("name", "?")
        pid = inst.get("pid", "?")
        toolset = inst.get("toolset", "?")
        uptime = _format_uptime(inst.get("started_at", time.time()))
        print(f"  {name:<14}{pid:<8}{toolset:<17}{uptime:<10}")

    print()
    count = len(instances)
    print(f" {count} instance{'s' if count != 1 else ''}")
    print()
