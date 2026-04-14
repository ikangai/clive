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
import shlex
import subprocess
from dataclasses import dataclass, field

from agents import _FORWARD_ENVS, _load_registry


def _extract_python_interpreter(clive_path: str) -> str:
    """Return the Python interpreter to use for the clive_installed check.

    If the first token of ``clive_path`` looks like a Python interpreter
    (basename starts with "python"), honour it — covers versioned
    interpreters (`python3.11`), venv paths (`/opt/app/.venv/bin/python`),
    and pyenv shims. Otherwise fall back to `python3`: the user's
    ``path`` points at a wrapper script or a binary that isn't a Python
    interpreter, and using it with ``-c`` would silently fail. The
    clive_installed check's purpose is "can the remote import clive",
    which is orthogonal to how clive is normally launched on that host.
    """
    try:
        tokens = shlex.split(clive_path)
    except ValueError:
        return "python3"
    if not tokens:
        return "python3"
    first = tokens[0]
    basename = os.path.basename(first)
    if basename.startswith("python"):
        return first
    return "python3"


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

    # 3. Remote clive is importable. Honour the user's ``path``
    # config when it points at a recognizable Python interpreter
    # (covers venv installs, versioned pythons, pyenv shims); fall
    # back to `python3` for wrapper scripts and bare binary names.
    # See _extract_python_interpreter.
    clive_path = config.get("path", "python3 clive.py")
    interp = _extract_python_interpreter(clive_path)
    import_cmd = ssh_base + [
        actual_host,
        f"{interp} -c 'import clive; print(\"ok\")'",
    ]
    try:
        r = subprocess.run(import_cmd, capture_output=True, text=True, timeout=10)
        result.checks["clive_installed"] = (
            r.returncode == 0 and "ok" in r.stdout,
            r.stderr.strip() or "ok",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        result.checks["clive_installed"] = (False, str(e))

    # 4. AcceptEnv check. We need to distinguish three outcomes:
    #
    #   (a) sshd -T failed (typically "must be run as root" on
    #       distros that require sudo). Report could-not-verify.
    #   (b) sshd -T succeeded AND has AcceptEnv directives. Parse
    #       them and check each _FORWARD_ENVS entry.
    #   (c) sshd -T succeeded BUT sshd_config has no AcceptEnv
    #       directive at all. Every outer-set var IS genuinely
    #       missing; report missing.
    #
    # We distinguish (a) from (c) by capturing sshd -T's exit code
    # in a sentinel echoed to stdout after the command. Both outcomes
    # produce "no AcceptEnv lines" without this disambiguation — the
    # Phase 3.5 fix conflated them and misreported (c) as could-not-
    # verify.
    accept_cmd = ssh_base + [
        actual_host,
        "sshd -T 2>/dev/null; echo '<<SSHD_EXIT>>' $?",
    ]
    _SSHD_MARKER = "<<SSHD_EXIT>>"
    try:
        r = subprocess.run(accept_cmd, capture_output=True, text=True, timeout=10)
        # Find the exit marker. It's always the last line of stdout.
        lines = r.stdout.splitlines()
        sshd_exit = None
        for line in reversed(lines):
            if line.startswith(_SSHD_MARKER):
                try:
                    sshd_exit = int(line[len(_SSHD_MARKER):].strip())
                except ValueError:
                    pass
                break

        if sshd_exit is None:
            # Shell pipeline didn't even run — the marker should
            # always be present. Conservative fallback.
            result.checks["accept_env"] = (
                True,
                "could not verify (sshd -T marker missing from output)",
            )
        elif sshd_exit != 0:
            # Case (a): sshd -T failed. Most commonly "must be run
            # as root" on distros that enforce it.
            result.checks["accept_env"] = (
                True,
                f"could not verify (sshd -T exited {sshd_exit}, "
                f"typically means sudo is required on the remote)",
            )
        else:
            # Cases (b) and (c): sshd -T succeeded. Collect AcceptEnv
            # directive lines from the output (stripping the marker).
            acceptenv_lines = [
                ln.lower() for ln in lines
                if ln.strip().lower().startswith("acceptenv")
            ]
            accepted_lc = "\n".join(acceptenv_lines)
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
