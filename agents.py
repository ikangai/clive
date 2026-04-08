"""Agent addressing and resolution for clive@host communication.

Parses clive@host addresses from task text, resolves them via a YAML
registry (~/.clive/agents.yaml) or auto-resolve fallback, and builds
SSH commands with API key forwarding (BYOLLM).

Address format: clive@<host> where host is [\\w.\\-]+
Registry: ~/.clive/agents.yaml (optional)
SSH: no -t flag (no TTY) → inner clive auto-detects conversational mode
"""
import os
import re

DEFAULT_REGISTRY = os.path.expanduser("~/.clive/agents.yaml")
DEFAULT_CLIVE_PATH = "python3 clive.py"

# Env vars to forward via SSH SendEnv (BYOLLM)
_FORWARD_ENVS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "LLM_PROVIDER",
    "AGENT_MODEL",
]

_ADDR_RE = re.compile(r"clive@([\w.\-]+)")


def parse_agent_addresses(task: str) -> list[tuple[str, str]]:
    """Extract clive@host addresses from task text.

    Returns list of (host, remaining_task) tuples.
    The clive@host token is stripped from the remaining task.
    """
    matches = list(_ADDR_RE.finditer(task))
    if not matches:
        return []

    results = []
    for match in matches:
        host = match.group(1)
        remaining = task[:match.start()] + task[match.end():]
        remaining = re.sub(r"\s+", " ", remaining).strip()
        results.append((host, remaining))

    return results


def _load_registry(path: str | None = None) -> dict:
    """Load agents.yaml registry. Returns empty dict if not found."""
    path = path or DEFAULT_REGISTRY
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_agent(host: str, registry_path: str | None = None) -> dict:
    """Resolve a clive@host address to a pane definition dict.

    Checks registry first, falls back to auto-resolve.
    Returns dict compatible with PANES entries in toolsets.py.
    """
    registry = _load_registry(registry_path)
    config = registry.get(host, {})

    actual_host = config.get("host", host)
    cmd = build_agent_ssh_cmd(actual_host, config)

    return {
        "name": f"agent-{host}",
        "cmd": cmd,
        "app_type": "agent",
        "description": (
            f"Remote clive instance at {actual_host}. "
            f"Peer conversation via TURN: protocol."
        ),
        "host": actual_host,
        "connect_timeout": config.get("timeout", 5),
        "category": "agent",
    }


def build_agent_ssh_cmd(host: str, config: dict) -> str:
    """Build SSH command for clive-to-clive connection.

    No -t flag (no TTY) → inner clive auto-detects conversational mode.
    Forwards API key env vars via SendEnv (BYOLLM).
    """
    parts = ["ssh"]

    # SSH key
    key = config.get("key")
    if key:
        parts.append(f"-i {key}")

    # Forward API key env vars
    for env_var in _FORWARD_ENVS:
        if os.environ.get(env_var):
            parts.append(f"-o SendEnv={env_var}")

    # Connection options
    parts.extend(["-o BatchMode=yes", "-o ConnectTimeout=10"])

    # Host
    parts.append(host)

    # Remote command
    clive_path = config.get("path", DEFAULT_CLIVE_PATH)
    toolset = config.get("toolset")
    remote_parts = [clive_path, "--conversational"]
    if toolset:
        remote_parts.extend(["-t", toolset])

    remote_cmd = " ".join(remote_parts)
    parts.append(f"'{remote_cmd}'")

    return " ".join(parts)
