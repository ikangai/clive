# server/auth.py
"""Token-based authentication for agent-to-agent communication.

Complements SSH key auth with a bearer token layer:
- Tokens stored in ~/.clive/agents.yaml per host
- Forwarded as CLIVE_AUTH_TOKEN env var via SSH SendEnv
- Remote clive validates token before accepting tasks
"""

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class AuthResult:
    allowed: bool
    reason: str = ""


def generate_token() -> str:
    """Generate a secure random token for agent authentication."""
    return secrets.token_urlsafe(48)


def validate_token(token: str, expected: str | None) -> AuthResult:
    """Validate a token against the expected value.

    Args:
        token: the token to validate
        expected: the expected token, or None if auth is disabled

    Returns:
        AuthResult indicating whether the token is valid
    """
    if expected is None:
        return AuthResult(allowed=True, reason="Auth disabled")
    if not token:
        return AuthResult(allowed=False, reason="Empty token provided")
    if not secrets.compare_digest(token, expected):
        return AuthResult(allowed=False, reason="Token mismatch — invalid credentials")
    return AuthResult(allowed=True, reason="Token valid")


def load_agent_tokens(path: str) -> dict[str, str]:
    """Load per-host agent tokens from a YAML file.

    Returns dict mapping hostname to token. Empty dict if file missing.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        import yaml
        with open(p) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return {}
        # Extract tokens from agent configs
        tokens = {}
        for host, config in data.items():
            if isinstance(config, dict) and "token" in config:
                tokens[host] = config["token"]
            elif isinstance(config, str):
                tokens[host] = config
        return tokens
    except ImportError:
        log.warning("PyYAML not installed, cannot load agent tokens")
        return {}
    except Exception as e:
        log.warning("Failed to load agent tokens from %s: %s", path, e)
        return {}


def save_agent_token(path: str, host: str, token: str):
    """Save a token for a host to the agents YAML file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if p.exists():
        try:
            import yaml
            with open(p) as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            pass

    if not isinstance(existing, dict):
        existing = {}

    existing[host] = {"token": token}

    try:
        import yaml
        with open(p, "w") as f:
            yaml.dump(existing, f, default_flow_style=False)
    except ImportError:
        # Fallback: write simple format without PyYAML
        with open(p, "w") as f:
            for h, conf in existing.items():
                if isinstance(conf, dict):
                    f.write(f"{h}:\n  token: {conf['token']}\n")
                else:
                    f.write(f"{h}: {conf}\n")
