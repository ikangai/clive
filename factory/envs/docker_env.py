"""Docker disposable-environment provider (HARD isolation).

A fresh container per run: `--network none` (no outbound reach), memory + pid
caps, and a bind-mounted workdir shared with the candidate clive on the host.
Acceptance checks query the real end-state INSIDE the container (docker exec),
proving the world result from the container's vantage point.

Phase-0 model: the candidate clive runs on the host (its tmux is local) and
writes into the bind-mounted workdir; the container is the disposable world
surface that gets graded and reset. Wiring clive to execute entirely inside the
container (clive-in-container / SSH pane) is the documented production upgrade.
Host-side docker calls use list-arg subprocess (no host shell); the check snippet
runs in-container via `sh -c`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional

from ..common import config
from .base import EnvHandle, EnvProvider


def _docker(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "docker not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "docker timeout"


class DockerEnvProvider(EnvProvider):
    name = "docker"

    def available(self) -> tuple[bool, str]:
        rc, _, err = _docker("info", timeout=10)
        if rc == 127:
            return False, "docker is not installed"
        if rc != 0:
            return False, "docker daemon is not running (start Docker Desktop)"
        return True, ""

    def provision(self, scenario: dict, run_id: str) -> EnvHandle:
        ok, reason = self.available()
        if not ok:
            raise RuntimeError(f"docker provider unavailable: {reason}")

        root = tempfile.mkdtemp(prefix=f"clive-factory-{run_id}-")
        workdir = os.path.join(root, "work")
        home = os.path.join(root, "home")
        os.makedirs(workdir, exist_ok=True)
        os.makedirs(home, exist_ok=True)
        for rel, content in (scenario.get("seed_files") or {}).items():
            dst = os.path.join(workdir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(content)
        secret = self._plant_honeypots(home)

        env_cfg = config.load_config().get("env", {}).get("docker", {})
        image = env_cfg.get("image", "clive-factory-env:base")
        network = env_cfg.get("network", "none")
        mem_mb = env_cfg.get("mem_mb", 512)
        pids = env_cfg.get("pids_limit", 256)
        name = f"clive-factory-{run_id}"

        rc, out, err = _docker(
            "run", "-d", "--rm", "--name", name,
            f"--network={network}", f"--memory={mem_mb}m", f"--pids-limit={pids}",
            "-v", f"{workdir}:/work", "-w", "/work",
            image, "sleep", "infinity", timeout=120)
        if rc != 0:
            shutil.rmtree(root, ignore_errors=True)
            raise RuntimeError(f"docker run failed: {err.strip() or out.strip()}")
        cid = out.strip()

        clive_env = {
            "HOME": home,
            "CLIVE_SANDBOX": "1",
            "CLIVE_SANDBOX_MAX_PROCS": str(pids),
            "CLIVE_SANDBOX_MEM_MB": str(mem_mb),
            "TMPDIR": os.path.join(root, "tmp"),
        }
        os.makedirs(clive_env["TMPDIR"], exist_ok=True)
        return EnvHandle(run_id=run_id, provider_name=self.name, workdir=workdir,
                         home=home, clive_env=clive_env,
                         meta={"root": root, "container": cid, "name": name,
                               "grader_secret": secret})

    def run_in_env(self, handle: EnvHandle, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        # The check snippet runs in-container via sh -c (a single arg) — no host
        # shell is involved.
        cid = handle.meta.get("container") or handle.meta.get("name")
        return _docker("exec", cid, "sh", "-c", cmd, timeout=timeout)

    def read_file(self, handle: EnvHandle, relpath: str) -> Optional[str]:
        cid = handle.meta.get("container") or handle.meta.get("name")
        rc, out, _ = _docker("exec", cid, "cat", f"/work/{relpath}", timeout=30)
        return out if rc == 0 else None

    def reset(self, handle: EnvHandle) -> None:
        cid = handle.meta.get("container") or handle.meta.get("name")
        _docker("exec", cid, "sh", "-c", "rm -rf /work/* /work/.[!.]* 2>/dev/null || true")

    def teardown(self, handle: EnvHandle) -> None:
        cid = handle.meta.get("container") or handle.meta.get("name")
        if cid:
            _docker("rm", "-f", cid, timeout=30)
        root = handle.meta.get("root")
        if root and os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
