"""Agent addressing and resolution for clive@host communication.

Parses clive@host addresses from task text, resolves them via a YAML
registry (~/.clive/agents.yaml) or auto-resolve fallback, and builds
SSH commands with API key forwarding (BYOLLM).

Address format: clive@<host>, host matching [A-Za-z0-9][\\w.\\-]* — it must
    start alphanumeric so attacker-influenceable task text cannot produce a
    "host" beginning with '-', which ssh would read as an option.
Registry: ~/.clive/agents.yaml (optional)
SSH: no -t flag (no TTY) → inner clive auto-detects conversational mode

Identity (see resolve_agent_user): the remote account is opt-in via
``user:`` in the host entry, ``CLIVE_AGENT_USER``, or
``CLIVE_AGENT_USER_PREFIX`` + the local principal. The principal itself
comes from ``CLIVE_PRINCIPAL_OVERRIDE``/``USER``/``LOGNAME`` and is
forwarded to the remote as ``CLIVE_PRINCIPAL`` for attribution.

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
import shlex
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

# Host must START alphanumeric. Without the anchor a task containing
# "clive@-Fevil_config" yields a "host" beginning with '-', which ssh
# parses as an OPTION rather than a destination — the task text is
# attacker-influenced, so it must not be able to inject ssh flags.
_ADDR_RE = re.compile(r"clive@([A-Za-z0-9][\w.\-]*)")

# ─── Agent identity ─────────────────────────────────────────────────────────
#
# clive acts ON BEHALF OF a person, and the two should not be the same
# POSIX principal. Logging in as the human is `su` semantics: the remote
# audit trail says the human did it, there is no way to revoke the agent
# without revoking the person, and every agent sharing one account can
# ride the others' ControlMaster sockets (they live in a single $HOME).
#
# Logging in as a dedicated account is `sudo` semantics — the actor is
# clive-<someone>, and CLIVE_PRINCIPAL records for whom. That makes
# attribution, revocation and least privilege expressible with plain
# users, groups and sudoers instead of application code.
#
# Default behavior is UNCHANGED (inherit the local username) so existing
# deployments keep working; opt in per host or globally.

AGENT_USER_ENV = "CLIVE_AGENT_USER"
AGENT_USER_PREFIX_ENV = "CLIVE_AGENT_USER_PREFIX"
PRINCIPAL_ENV = "CLIVE_PRINCIPAL"

# Portable POSIX username (see useradd(8) NAME_REGEX). Deliberately strict:
# this value is interpolated into an ssh destination.
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# The principal is interpolated into the remote command string, which is
# itself wrapped in single quotes and then parsed by the LOCAL shell before
# ssh ever sees it. shlex.quote() is NOT sufficient there: it protects one
# shell layer, and its own quotes terminate the outer ones, so a principal
# containing a quote makes the command unparseable and one containing ';'
# escapes to the local shell. Validate instead of quoting — same reasoning
# as _USER_RE, and it avoids the false assurance a quote call would give.
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def local_principal() -> str:
    """The local human this agent is acting on behalf of.

    ``CLIVE_PRINCIPAL_OVERRIDE`` wins (tests, and hosts where $USER is not
    the human), then ``USER``/``LOGNAME``. A value outside
    :data:`_PRINCIPAL_RE` degrades to ``"unknown"`` rather than raising:
    this is attribution metadata, and a weird local username should not
    make the agent unable to connect.
    """
    raw = (
        os.environ.get("CLIVE_PRINCIPAL_OVERRIDE")
        or os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or "unknown"
    )
    return raw if _PRINCIPAL_RE.match(raw) else "unknown"


def resolve_agent_user(config: dict | None = None) -> str | None:
    """Resolve the remote POSIX account to log in as, or None to inherit.

    Order: explicit ``user:`` in the host's agents.yaml entry, then
    ``CLIVE_AGENT_USER``, then ``CLIVE_AGENT_USER_PREFIX`` + the local
    principal (so ``clive-`` yields ``clive-martin``). The principal is
    lowercased when derived, since ``_USER_RE`` is lowercase-only and a
    capitalized ``$USER`` would otherwise fail at connection time.

    None means "use ssh's default", which is the pre-existing behavior.
    An *empty* setting means the same: ``CLIVE_AGENT_USER=""`` is the
    conventional way to disable an env var in a shell or CI config, so it
    must read as "inherit", not as an invalid username.
    """
    config = config or {}
    user = config.get("user") or os.environ.get(AGENT_USER_ENV)
    if not user:
        prefix = os.environ.get(AGENT_USER_PREFIX_ENV)
        if prefix:
            user = f"{prefix}{local_principal().lower()}"
    if not user:
        return None
    user = str(user)
    if not _USER_RE.match(user):
        raise ValueError(
            f"invalid agent user {user!r}: must match {_USER_RE.pattern} "
            f"(it is interpolated into an ssh destination)"
        )
    return user


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
    # These come from a file on disk and are interpolated into a command
    # that is typed into a shell by send_keys, so quote them. Without this
    # a writable registry entry is arbitrary command execution in the
    # outer's pane the next time the user addresses this instance (gh#48).
    tmux_session = shlex.quote(str(inst["tmux_session"]))
    tmux_socket = shlex.quote(str(inst["tmux_socket"]))
    # Thread the instance's published nonce so the pane reader can
    # authenticate its frames. Absent (an instance from before gh#46, or one
    # that never published one) this stays empty and decode_all fails closed
    # — the conversation degrades to plain text rather than silently
    # trusting anything printed on the pane.
    nonce = str(inst.get("frame_nonce") or "")
    if not nonce:
        log.warning(
            "local instance %r published no frame nonce; its framed protocol "
            "will be ignored (restart it to publish one)", name
        )
    return {
        "name": f"agent-{name}",
        "cmd": f"tmux -L {tmux_socket} attach -t {tmux_session}:conversational",
        "app_type": "agent",
        "description": f"Local clive instance '{name}'",
        "host": None,
        "category": "agent",
        "frame_nonce": nonce,
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

    # Destination. With an agent account this is clive-<principal>@host,
    # so the remote's auth.log names the agent rather than the human.
    agent_user = resolve_agent_user(config)
    parts.append(f"{agent_user}@{host}" if agent_user else host)

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

    # Delegation with attribution: the ACTOR is the agent account, the
    # PRINCIPAL is the person it works for. Carrying it lets the remote
    # log "clive-martin, on behalf of martin" rather than losing the
    # human at the door. Not a credential — it asserts nothing on its
    # own, and sshd already authenticated the actor.
    # Not shlex.quote()d — see _PRINCIPAL_RE. local_principal() is already
    # constrained to a shell-inert charset, and quoting here would nest
    # inside the outer single quotes added below and break them.
    remote_parts.append(f"{PRINCIPAL_ENV}={local_principal()}")

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
