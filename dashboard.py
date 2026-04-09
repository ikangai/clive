"""Dashboard snapshot for clive instances.

Reads the instance registry, prunes dead PIDs, and prints a table
similar to `docker ps`. Designed to be called from `clive --dashboard`
or from the TUI `/dashboard` command.
"""
import os
import time
from pathlib import Path

from registry import list_instances


def _format_uptime(started_at: float) -> str:
    elapsed = max(0, time.time() - started_at)
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    return f"{hours}h {minutes:02d}m"


def _load_remote_agents(agents_yaml_path: str | None = None) -> list[dict]:
    """Load remote agents from agents.yaml for display."""
    path = agents_yaml_path or os.path.expanduser("~/.clive/agents.yaml")
    if not os.path.exists(path):
        return []
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return []
        result = []
        for name, config in data.items():
            result.append({
                "name": name,
                "host": config.get("host", name),
                "toolset": config.get("toolset", "?"),
            })
        return result
    except Exception:
        return []


def render_snapshot(registry_dir: Path | None = None,
                    agents_yaml_path: str | None = None) -> None:
    instances = list_instances(registry_dir=registry_dir)
    remote_agents = _load_remote_agents(agents_yaml_path)

    if not instances and not remote_agents:
        print("No instances running.")
        return

    if instances:
        # Header
        print()
        print(" CLIVE INSTANCES")
        print(" " + "─" * 55)
        print(f"  {'NAME':<14}{'PID':<8}{'TOOLSET':<17}{'STATUS':<10}{'UPTIME':<10}")

        for inst in instances:
            name = inst.get("name", "?")
            pid = inst.get("pid", "?")
            toolset = inst.get("toolset", "?")
            status = "idle"  # TODO: tmux pane peek for TURN: state (deferred to v2)
            uptime = _format_uptime(inst.get("started_at", time.time()))
            print(f"  {name:<14}{pid:<8}{toolset:<17}{status:<10}{uptime:<10}")

    if remote_agents:
        print()
        print(" REMOTE AGENTS")
        print(" " + "─" * 55)
        print(f"  {'NAME':<14}{'HOST':<25}{'TOOLSET':<17}")
        for agent in remote_agents:
            name = agent.get("name", "?")
            host = agent.get("host", "?")
            toolset = agent.get("toolset", "?")
            print(f"  {name:<14}{host:<25}{toolset:<17}")

    # Summary footer
    print()
    n_local = len(instances)
    n_remote = len(remote_agents)
    parts = []
    parts.append(f"{n_local} instance{'s' if n_local != 1 else ''}")
    if n_remote:
        parts.append(f"{n_remote} remote")
    print(f" {' · '.join(parts)}")
    print()
