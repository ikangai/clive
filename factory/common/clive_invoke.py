"""Build + run the candidate clive invocation for one (spec, model, env, goal).

Composition of the process environment (later overrides earlier):
  1. inherited os.environ (so clive's .env / API keys load normally)
  2. env-provider vars     (HOME -> sandbox, CLIVE_SANDBOX, isolation)
  3. applied-spec vars     (the candidate's open block actuated -> clive knobs)
  4. panel-model vars      (LLM_PROVIDER / AGENT_MODEL / SCRIPT_MODEL / CLASSIFIER_MODEL)
  5. always: CLIVE_KEEP_SESSION=1 (keep evidence)

argv: <clive_python> <clive.py> -q --json --max-tokens <N> <spec flags> <goal>
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config

# Host credentials/state a candidate must NOT inherit. The LLM provider key (the
# "intelligence" credential) is intentionally NOT scrubbed — clive needs it to
# think, and the spec's isolation boundary is the ENVIRONMENT, not the brain.
# These are non-LLM secrets + clive flags that could reach a real system.
_SCRUB_EXACT = {
    "CLIVE_EXPERIMENTAL_SELFMOD", "CLIVE_AUTO_EXPLORE", "CLIVE_TRUST_UNREVIEWED",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN", "GH_TOKEN", "NPM_TOKEN", "DOCKER_AUTH_CONFIG",
    "KUBECONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "SSH_AUTH_SOCK",
}
_SCRUB_PREFIX = ("AWS_", "GH_", "GITHUB_", "DOCKER_", "KUBE", "VAULT_", "GCP_")

_SESSION_RE = re.compile(r"Session:\s*(/tmp/clive/\S+)")


def parse_session_dirs(text: str) -> list[str]:
    """Recover this run's clive session dir(s) from its stderr (`Session: /tmp/clive/<id>`)
    so evidence collection is scoped to THIS run, not the global /tmp/clive."""
    return list(dict.fromkeys(_SESSION_RE.findall(text or "")))


def _scrub_env(env: dict[str, str]) -> None:
    for k in list(env.keys()):
        if k in _SCRUB_EXACT or any(k.startswith(p) for p in _SCRUB_PREFIX):
            env.pop(k, None)


@dataclass
class CliveResult:
    rc: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    argv: list[str]
    env_overrides: dict[str, str]


def panel_env(model_entry: dict) -> dict[str, str]:
    """Map a panel model entry to clive's model-selection env. We point all model
    tiers at one provider/model so only one credential is needed per panel run."""
    provider = model_entry.get("provider", "openrouter")
    model = model_entry.get("model", "")
    env = {"LLM_PROVIDER": provider}
    if model:
        env["AGENT_MODEL"] = model
        env["SCRIPT_MODEL"] = model
        # Use the same model for the tier-1 classifier so we don't implicitly
        # depend on a second provider's key (clive defaults CLASSIFIER_MODEL to
        # a gemini model). "none" would disable tier-1 entirely.
        env["CLASSIFIER_MODEL"] = model
    if model_entry.get("base_url"):
        env["LLM_BASE_URL"] = model_entry["base_url"]
    return env


def build(goal: str, *, applied_env: dict[str, str], applied_flags: list[str],
          env_vars: dict[str, str], model_entry: dict,
          max_tokens: int) -> tuple[list[str], dict[str, str]]:
    clive_root, clive_py = config.clive_entry()
    py = config.clive_python()

    env = dict(os.environ)
    _scrub_env(env)                   # drop non-LLM host creds + dangerous clive flags
    env.update(env_vars or {})        # env-provider (HOME, CLIVE_SANDBOX, ...)
    env.update(applied_env or {})     # candidate open block actuated
    env.update(panel_env(model_entry))
    env["CLIVE_KEEP_SESSION"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # Self-modification must never reach the real clive source from a candidate.
    # load_dotenv(override=False) means this 0 survives clive/.env's =1, and
    # --safe-mode (below) forces it off inside clive regardless.
    env["CLIVE_EXPERIMENTAL_SELFMOD"] = "0"

    argv = [py, clive_py, "-q", "--json", "--safe-mode", "--max-tokens", str(max_tokens)]
    argv += list(applied_flags or [])
    argv += [goal]
    return argv, env


def run(goal: str, *, applied_env, applied_flags, env_vars, model_entry,
        max_tokens: int, timeout_s: int, cwd: Optional[str] = None) -> CliveResult:
    argv, env = build(goal, applied_env=applied_env, applied_flags=applied_flags,
                      env_vars=env_vars, model_entry=model_entry,
                      max_tokens=max_tokens)
    clive_root, _ = config.clive_entry()
    workdir = cwd or clive_root  # clive.py must run from the repo root (sys.path shim)
    # NB: clive's shell pane CWD is the sandbox via HOME + the goal's paths; the
    # process itself runs from the clive repo root so its imports resolve.
    start = time.time()
    timed_out = False
    try:
        proc = subprocess.run(argv, env=env, cwd=workdir, capture_output=True,
                              text=True, timeout=timeout_s)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = 124
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")) \
            + "\n[factory] candidate clive exceeded per-run timeout"
    dur = time.time() - start
    # Only the overrides (not the whole inherited environment) for the evidence log.
    overrides = {k: env[k] for k in (
        list((env_vars or {}).keys()) + list((applied_env or {}).keys())
        + list(panel_env(model_entry).keys())
        + ["CLIVE_KEEP_SESSION", "CLIVE_EXPERIMENTAL_SELFMOD"]) if k in env}
    return CliveResult(rc=rc, stdout=out, stderr=err, duration_s=dur,
                       timed_out=timed_out, argv=argv, env_overrides=overrides)
