"""Agent addressing and resolution for clive@host communication.

Parses clive@host addresses from task text, resolves them via a YAML
registry (~/.clive/agents.yaml) or auto-resolve fallback, and builds
SSH commands with API key forwarding (BYOLLM).

Address format: clive@<host> where host is [\\w.\\-]+
Registry: ~/.clive/agents.yaml (optional)
SSH: no -t flag (no TTY) → inner clive auto-detects conversational mode

Session nonce (see protocol.py): every SSH invocation generates a fresh
random nonce and injects it into the remote env as CLIVE_FRAME_NONCE.
The inner's encode() picks it up automatically; the outer keeps the
nonce on the returned pane_def so its pane reader can authenticate
frames from that specific inner. This closes the "LLM inside inner
fabricates a fake protocol frame" attack surface.
"""
import logging
import os
import re
from pathlib import Path

from protocol import generate_nonce
from registry import get_instance

log = logging.getLogger(__name__)

DEFAULT_REGISTRY = os.path.expanduser("~/.clive/agents.yaml")
DEFAULT_CLIVE_PATH = "python3 clive.py"


def ensure_ssh_control_dir() -> str | None:
    """Create ~/.clive/ssh/ if missing. Return its absolute path, or
    None if creation failed (e.g. ~/.clive exists as a regular file).

    SSH ControlMaster connection pooling is an optimization, not a
    correctness feature — when the directory can't be created, we
    degrade to unpooled connections instead of crashing.
    build_agent_ssh_cmd checks os.path.isdir(ctl_dir) independently
    and skips the ControlMaster options when it's missing.

    Idempotent: repeated calls return the same path without
    re-creating the directory. resolve_agent() calls this on every
    invocation so any entry point that reaches agent addressing
    (clive.py __main__, future tui.py wiring, CLI scripts, etc.)
    gets pooling without needing a dedicated startup hook.
    """
    ctl_dir = os.path.expanduser("~/.clive/ssh")
    try:
        os.makedirs(ctl_dir, exist_ok=True, mode=0o700)
        return ctl_dir
    except OSError as e:
        # PermissionError is a subclass of OSError, as is
        # NotADirectoryError (when ~/.clive is a regular file).
        log.warning(
            "could not create SSH control dir %s: %s — "
            "falling back to unpooled SSH connections",
            ctl_dir, e,
        )
        return None

# Env vars to forward via SSH SendEnv (BYOLLM)
_FORWARD_ENVS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "LLM_PROVIDER",
    "AGENT_MODEL",
    "LLM_BASE_URL",
]

# Outer provider names that cannot be reached from a remote host
# without network tunneling. When the outer is on one of these, we
# transparently switch the remote to LLM_PROVIDER=delegate so the
# inner routes inference back through the conversational channel.
_LOCAL_PROVIDERS = frozenset({"lmstudio", "ollama"})

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


def _check_local_registry(name: str, instance_registry_dir: Path | None = None) -> dict | None:
    """Check local instance registry for a live, conversational instance.

    Returns a pane definition dict if found, None otherwise.
    """
    inst = get_instance(name, registry_dir=instance_registry_dir)
    if inst is None or not inst.get("conversational"):
        return None
    tmux_session = inst["tmux_session"]
    tmux_socket = inst["tmux_socket"]
    return {
        "name": f"agent-{name}",
        "cmd": f"tmux -L {tmux_socket} attach -t {tmux_session}:conversational",
        "app_type": "agent",
        "description": f"Local clive instance '{name}'",
        "host": None,
        "category": "agent",
    }


def resolve_agent(host: str, registry_path: str | None = None,
                   instance_registry_dir: Path | None = None) -> dict:
    """Resolve a clive@host address to a pane definition dict.

    Resolution order:
    1. Local instance registry (~/.clive/instances/) — live, conversational instances
    2. Remote agents.yaml registry — SSH-based resolution
    3. Auto-resolve fallback — direct SSH to host

    Returns dict compatible with PANES entries in toolsets.py.
    """
    # Step 1: Check local instance registry
    local = _check_local_registry(host, instance_registry_dir)
    if local is not None:
        return local

    # Step 2+3: Remote registry / auto-resolve
    registry = _load_registry(registry_path)
    config = registry.get(host, {})

    # Ensure the SSH control-socket directory exists before we build
    # a command that references it. Idempotent — cheap on repeat
    # calls, and covers every entry point (clive.py, tui.py, one-shot
    # CLI scripts, scheduler jobs) without needing per-entry startup
    # hooks. If the helper returns None (unlikely edge case like
    # ~/.clive is a file), build_agent_ssh_cmd's isdir check will
    # skip the ControlMaster options and we degrade to unpooled SSH.
    ensure_ssh_control_dir()

    actual_host = config.get("host", host)
    nonce = generate_nonce()
    cmd = build_agent_ssh_cmd(actual_host, config, nonce=nonce)

    return {
        "name": f"agent-{host}",
        "cmd": cmd,
        "app_type": "agent",
        "description": (
            f"Remote clive instance at {actual_host}. "
            f"Peer conversation via framed protocol (see protocol.py)."
        ),
        "host": actual_host,
        "connect_timeout": config.get("timeout", 5),
        "category": "agent",
        "frame_nonce": nonce,
    }


def build_agent_ssh_cmd(host: str, config: dict, nonce: str | None = None) -> str:
    """Build SSH command for clive-to-clive connection.

    No -t flag (no TTY) → inner clive auto-detects conversational mode.
    Forwards API key env vars via SendEnv (BYOLLM).

    ``nonce`` is the session nonce injected into the remote env as
    CLIVE_FRAME_NONCE so the inner's framed-protocol emitters carry
    an authenticated value. If None, a fresh nonce is generated; pass
    an explicit value when you also need to remember it (e.g. on the
    pane_def) — see resolve_agent().
    """
    if nonce is None:
        nonce = generate_nonce()

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

    # Connection pooling: reuse a single SSH channel for rapid agent
    # traffic (delegate round trips, scp, reconnects). Control sockets
    # live under ~/.clive/ssh/ (created at clive startup via
    # ensure_ssh_control_dir, not as a side effect of this function).
    # If the directory does not exist on disk, skip the ControlMaster
    # options — SSH pooling is an optimization, not a correctness
    # feature, and we prefer unpooled over a crash.
    ctl_dir = os.path.expanduser("~/.clive/ssh")
    if os.path.isdir(ctl_dir):
        parts.extend([
            "-o ControlMaster=auto",
            f"-o ControlPath={ctl_dir}/%C",
            "-o ControlPersist=60s",
        ])

    # Host
    parts.append(host)

    # Remote command. Env-var assignments are prefixed on the remote
    # command itself (not SSH SendEnv / AcceptEnv) so we do not depend
    # on sshd config on the remote.
    #   - CLIVE_FRAME_NONCE authenticates the framed protocol for
    #     this specific session; not sensitive beyond the session,
    #     acceptable to be visible in remote `ps` output.
    #   - LLM_PROVIDER=delegate + AGENT_MODEL=delegate force the inner
    #     to route inference back through the conversational channel
    #     when the outer is on a local-only provider (LMStudio,
    #     Ollama). Cloud providers are reachable from the remote
    #     directly and get their env vars forwarded via SendEnv above.
    clive_path = config.get("path", DEFAULT_CLIVE_PATH)
    toolset = config.get("toolset")
    remote_parts = [f"CLIVE_FRAME_NONCE={nonce}"]

    outer_provider = os.environ.get("LLM_PROVIDER", "").lower()
    if outer_provider in _LOCAL_PROVIDERS:
        remote_parts.append("LLM_PROVIDER=delegate")
        remote_parts.append("AGENT_MODEL=delegate")

    remote_parts.extend([clive_path, "--conversational"])
    if toolset:
        remote_parts.extend(["-t", toolset])

    remote_cmd = " ".join(remote_parts)
    parts.append(f"'{remote_cmd}'")

    return " ".join(parts)
