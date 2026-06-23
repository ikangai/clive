"""Local tempdir sandbox provider (Phase-0 default).

The environment is a throwaway directory tree. Isolation is a SOFT boundary:
  - HOME is relocated into the sandbox, so clive's ~/.clive, session log and
    session dirs land inside it (per-run isolation + evidence capture);
  - CLIVE_SANDBOX=1 engages clive's own command-execution sandbox
    (runtime.py:228) with process/memory caps.
This is honest about its limits: a determined candidate could still touch host
paths. The docker provider gives a HARD boundary. Negative safety checks
(checks/safety.py) catch out-of-scope reach in evidence either way.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional

from ..common import config
from .base import EnvHandle, EnvProvider


class LocalSandboxProvider(EnvProvider):
    name = "local"

    def provision(self, scenario: dict, run_id: str) -> EnvHandle:
        root = tempfile.mkdtemp(prefix=f"clive-factory-{run_id}-")
        workdir = os.path.join(root, "work")
        home = os.path.join(root, "home")
        os.makedirs(workdir, exist_ok=True)
        os.makedirs(home, exist_ok=True)
        # Seed any initial files declared by the scenario snapshot.
        for rel, content in (scenario.get("seed_files") or {}).items():
            dst = os.path.join(workdir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(content)
        secret = self._plant_honeypots(home)

        env_cfg = config.load_config().get("env", {})
        sandbox = env_cfg.get("docker", {})  # reuse mem/pids hints if present
        clive_env = {
            "HOME": home,
            "CLIVE_SANDBOX": "1",
            "CLIVE_SANDBOX_MAX_PROCS": str(sandbox.get("pids_limit", 256)),
            "CLIVE_SANDBOX_MEM_MB": str(sandbox.get("mem_mb", 512)),
            "TMPDIR": os.path.join(root, "tmp"),
        }
        os.makedirs(clive_env["TMPDIR"], exist_ok=True)
        return EnvHandle(run_id=run_id, provider_name=self.name, workdir=workdir,
                         home=home, clive_env=clive_env,
                         meta={"root": root, "grader_secret": secret})

    def run_in_env(self, handle: EnvHandle, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        # `cmd` is a factory-authored CHECK snippet (deterministic acceptance
        # checks need shell semantics: pipes, redirects, test). It is NEVER
        # candidate- or model-derived input, so shell=True is the intended
        # affordance here, not an injection surface.
        env = dict(os.environ)
        env["HOME"] = handle.home
        try:
            p = subprocess.run(cmd, shell=True, cwd=handle.workdir, env=env,
                               capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"

    def read_file(self, handle: EnvHandle, relpath: str) -> Optional[str]:
        path = os.path.join(handle.workdir, relpath)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def reset(self, handle: EnvHandle) -> None:
        # Reset to known initial state: clear workdir, keep honeypots in home.
        if os.path.isdir(handle.workdir):
            shutil.rmtree(handle.workdir, ignore_errors=True)
        os.makedirs(handle.workdir, exist_ok=True)

    def teardown(self, handle: EnvHandle) -> None:
        root = handle.meta.get("root")
        if root and os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
