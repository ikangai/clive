"""`clive --agents-doctor` — validate remote clive connectivity.

Runs a series of checks against each configured agent host and
produces a pass/fail report per check. The single biggest class of
production bugs in the remote-clive subsystem is silent misconfig:

  - SSH key path wrong or missing
  - BatchMode connect fails (host down, firewall, user mismatch)
  - clive not installed on the remote (or the wrong path)
  - AcceptEnv on the remote's sshd does not match the outer's
    SendEnv list, so API keys are silently dropped

The doctor surfaces all of these proactively in a single command so
users don't have to debug at 3am. Run it before deploying a new
remote agent, or when a previously-working one starts timing out.

Design notes:

- Every check is best-effort and independently reported. One failing
  check does NOT short-circuit the remaining ones — we want a full
  picture, not the first error.
- The sshd -T probe (for AcceptEnv) requires sudo on many distros,
  so "could not verify" is treated as OK, not failure — a false
  positive is less bad than a false negative here.
- Returns structured AgentCheck objects; formatting is separate so
  a future `--json` output mode is a one-liner.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from agents import _FORWARD_ENVS, _load_registry


@dataclass
class AgentCheck:
    host: str
    checks: dict = field(default_factory=dict)  # name -> (ok: bool, detail: str)

    def ok(self) -> bool:
        """True iff every individual check passed."""
        return all(v[0] for v in self.checks.values())


def check_agent(host: str, config: dict) -> AgentCheck:
    """Run all health checks against a single agent host entry."""
    result = AgentCheck(host=host)

    # 1. Key file exists (if specified)
    key = config.get("key")
    if key:
        expanded = os.path.expanduser(key)
        exists = os.path.exists(expanded)
        result.checks["key_exists"] = (
            exists,
            expanded if exists else f"missing: {expanded}",
        )
    else:
        result.checks["key_exists"] = (True, "using SSH default identity")

    # 2. SSH connectivity (5s timeout, non-interactive, BatchMode so we
    # never hang waiting for a password prompt)
    actual_host = config.get("host", host)
    ssh_base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if key:
        ssh_base.extend(["-i", os.path.expanduser(key)])
    connect_cmd = ssh_base + [actual_host, "echo clive-doctor-ok"]
    try:
        r = subprocess.run(connect_cmd, capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0 and "clive-doctor-ok" in r.stdout
        result.checks["ssh_connect"] = (ok, r.stderr.strip() or "ok")
    except (subprocess.TimeoutExpired, OSError) as e:
        result.checks["ssh_connect"] = (False, str(e))
        # No point running the remaining remote checks if we can't
        # even reach the host.
        return result

    # 3. Remote clive is importable. The check's purpose is "can the
    # remote find the clive module", which is independent of how
    # clive is normally launched on that host. Always use `python3 -c`,
    # NOT the configured `path` — a legitimate wrapper path like
    # `/opt/clive/bin/clive` is not a Python interpreter and would
    # silently fail the `-c` invocation.
    import_cmd = ssh_base + [
        actual_host,
        "python3 -c 'import clive; print(\"ok\")'",
    ]
    try:
        r = subprocess.run(import_cmd, capture_output=True, text=True, timeout=10)
        result.checks["clive_installed"] = (
            r.returncode == 0 and "ok" in r.stdout,
            r.stderr.strip() or "ok",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        result.checks["clive_installed"] = (False, str(e))

    # 4. AcceptEnv check — list what the remote sshd is configured to
    # accept, and verify every env var in _FORWARD_ENVS that is set on
    # the outer would actually be passed through.
    accept_cmd = ssh_base + [
        actual_host,
        "sshd -T 2>/dev/null | grep -i acceptenv || true",
    ]
    try:
        r = subprocess.run(accept_cmd, capture_output=True, text=True, timeout=10)
        accepted_lc = r.stdout.lower()
        if not accepted_lc.strip():
            # sshd -T requires sudo on most distros. When run as an
            # unprivileged user, sshd prints to stderr and exits non-
            # zero; our `2>/dev/null | grep ... || true` swallows both,
            # leaving stdout empty. Treat this as "could not verify"
            # instead of "every env var is missing" — a false negative
            # in the verification path, not the user-visible check.
            result.checks["accept_env"] = (
                True,
                "could not verify (remote sshd -T returned empty output — "
                "likely needs sudo to run)",
            )
        else:
            missing = [
                v for v in _FORWARD_ENVS
                if os.environ.get(v) and v.lower() not in accepted_lc
            ]
            if missing:
                result.checks["accept_env"] = (
                    False,
                    f"remote sshd missing AcceptEnv for: {', '.join(missing)}",
                )
            else:
                result.checks["accept_env"] = (True, "all set envs accepted")
    except Exception as e:
        # Not fatal — user may not have sudo on remote to read sshd -T,
        # or sshd may not be in PATH for their login shell. False
        # positives on AcceptEnv are less bad than false negatives.
        result.checks["accept_env"] = (True, f"could not verify ({e})")

    return result


def run_doctor(registry_path: str | None = None) -> list[AgentCheck]:
    """Load the agents registry and run check_agent() on each entry.

    When registry_path is None, uses the default ~/.clive/agents.yaml.
    Returns an empty list if no registry file exists — useful for
    first-run feedback.
    """
    registry = _load_registry(registry_path)
    return [check_agent(host, config) for host, config in registry.items()]


def format_report(results: list[AgentCheck]) -> str:
    """Render a list of AgentCheck results as a human-readable report.

    Empty input returns an empty string. Each host gets a header
    (✓ or ✗), followed by indented per-check lines with their
    individual status and detail.
    """
    if not results:
        return ""
    lines = []
    for r in results:
        status = "✓" if r.ok() else "✗"
        lines.append(f"{status} {r.host}")
        for name, (ok, detail) in r.checks.items():
            icon = "  ✓" if ok else "  ✗"
            lines.append(f"{icon} {name}: {detail}")
    return "\n".join(lines)
