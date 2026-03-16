"""Eval session fixture: isolated tmux + filesystem for reproducible evals.

Creates a fresh tmux session with a known filesystem state, runs the eval,
and tears everything down. Each eval gets its own session name and temp
directory to avoid interference.
"""
import os
import shutil
import tempfile
import time
import uuid

import libtmux

from models import PaneInfo


class EvalFixture:
    """Context manager for eval sessions.

    Usage:
        with EvalFixture(fixture_dir="evals/layer2/shell/fixtures/task_001") as ef:
            ef.send_keys("ls -la")
            screen = ef.capture()
            assert "file1.txt" in screen
    """

    def __init__(
        self,
        fixture_dir: str | None = None,
        pane_app_type: str = "shell",
        session_prefix: str = "clive_eval",
    ):
        self.fixture_dir = fixture_dir
        self.pane_app_type = pane_app_type
        self.session_name = f"{session_prefix}_{uuid.uuid4().hex[:8]}"
        self.workdir: str = ""
        self.session: libtmux.Session | None = None
        self.pane: libtmux.Pane | None = None
        self.pane_info: PaneInfo | None = None

    def __enter__(self):
        # Create isolated workdir
        self.workdir = tempfile.mkdtemp(prefix="clive_eval_")

        # Copy fixture files if provided
        if self.fixture_dir and os.path.isdir(self.fixture_dir):
            for item in os.listdir(self.fixture_dir):
                src = os.path.join(self.fixture_dir, item)
                dst = os.path.join(self.workdir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

        # Create tmux session
        server = libtmux.Server()
        self.session = server.new_session(
            session_name=self.session_name,
            kill_session=True,
            attach=False,
        )
        self.pane = self.session.active_window.active_pane

        # Set up shell environment
        self.pane.send_keys('export PS1="[AGENT_READY] $ "', enter=True)
        self.pane.send_keys(f'cd {self.workdir}', enter=True)
        time.sleep(0.5)

        self.pane_info = PaneInfo(
            pane=self.pane,
            app_type=self.pane_app_type,
            description=f"Eval pane ({self.pane_app_type})",
            name="eval",
            idle_timeout=2.0,
        )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Kill tmux session
        if self.session:
            try:
                self.session.kill()
            except Exception:
                pass

        # Remove workdir
        if self.workdir and os.path.exists(self.workdir):
            shutil.rmtree(self.workdir, ignore_errors=True)

        return False

    def send_keys(self, keys: str, enter: bool = True):
        """Send keys to the eval pane."""
        self.pane.send_keys(keys, enter=enter)

    def capture(self) -> str:
        """Capture current screen content."""
        lines = self.pane.cmd("capture-pane", "-p").stdout
        return "\n".join(lines) if lines else ""

    def wait_for_prompt(self, timeout: float = 5.0) -> str:
        """Wait for [AGENT_READY] prompt to appear, return screen."""
        start = time.time()
        while time.time() - start < timeout:
            screen = self.capture()
            lines = screen.strip().split("\n")
            if lines and "[AGENT_READY] $" in lines[-1]:
                return screen
            time.sleep(0.1)
        return self.capture()
