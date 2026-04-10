"""Tests for the conversational keepalive ticker.

An inner clive in `--conversational` mode sits on stdin waiting for
the next task. If the outer clive crashes, goes away, or the SSH
connection drops, the inner has no way to detect it — `readline()`
blocks forever. A background thread emitting `alive` frames every
~15 seconds lets any supervisor (the outer's pane reader, a StallDetector,
or a human debugging a wedged session) notice the inner is still
alive but also notice when it stops being alive.

These tests spawn real subprocesses because threading + stdin is
fundamentally a multi-process behaviour. They run without tmux setup
because the subprocess never reaches `run()` — it blocks in the
initial-readline branch of the conversational loop.
"""
import os
import select
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from protocol import decode_all


def _read_available_with_timeout(stream, timeout: float) -> str:
    """Read whatever is currently available on `stream` within `timeout`
    seconds. Returns empty string on no-data-yet (NOT EOF). Uses
    select() so the caller's deadline check can run between reads —
    a plain readline() would block indefinitely if the child is silent,
    defeating any wall-clock timeout the test is trying to enforce.

    This is the same lesson as DelegateClient's C1 fix from Phase 2.5.
    """
    try:
        ready, _, _ = select.select([stream], [], [], timeout)
    except (OSError, ValueError):
        return ""
    if not ready:
        return ""
    return stream.readline()


if not shutil.which("tmux"):
    pytest.skip("tmux not available — skipping keepalive integration tests",
                allow_module_level=True)


def _spawn_inner(extra_args=None, timeout=25):
    """Spawn `clive.py --conversational --name <unique>` as a subprocess.

    Returns the Popen handle. Caller must stop it when done.
    """
    repo_root = Path(__file__).parent.parent
    unique_name = f"keepalive-{uuid.uuid4().hex[:8]}"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # Force a provider that doesn't need API keys so startup doesn't fail.
    env["LLM_PROVIDER"] = "lmstudio"
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENROUTER_API_KEY", None)

    cmd = [
        sys.executable, "-u", str(repo_root / "clive.py"),
        "--conversational", "--name", unique_name,
    ]
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=0,
    )
    return proc, unique_name


def _shutdown(proc):
    try:
        proc.stdin.write("exit\n")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_conversational_loop_emits_alive_frames_while_waiting_on_stdin():
    """A named-instance inner with no initial task blocks on stdin for
    the first task. The alive ticker must be running during that wait,
    so alive frames appear on stdout within ~20 seconds (15s interval
    plus some slack for process startup)."""
    proc, _name = _spawn_inner()

    deadline = time.time() + 25
    buf = ""
    saw_alive = False
    try:
        while time.time() < deadline:
            chunk = _read_available_with_timeout(proc.stdout, 0.5)
            if chunk:
                buf += chunk
                frames = decode_all(buf)
                if any(f.kind == "alive" for f in frames):
                    saw_alive = True
                    break
    finally:
        _shutdown(proc)

    assert saw_alive, (
        f"no alive frame within 25s.\nstdout buf:\n{buf!r}\n"
        f"stderr:\n{proc.stderr.read() if proc.stderr else ''}"
    )


def test_alive_frame_payload_has_timestamp():
    """Each alive frame must carry a float `ts` field so supervisors
    can compute staleness."""
    proc, _name = _spawn_inner()

    deadline = time.time() + 25
    buf = ""
    alive_frame = None
    try:
        while time.time() < deadline:
            chunk = _read_available_with_timeout(proc.stdout, 0.5)
            if chunk:
                buf += chunk
                frames = decode_all(buf)
                alives = [f for f in frames if f.kind == "alive"]
                if alives:
                    alive_frame = alives[0]
                    break
    finally:
        _shutdown(proc)

    assert alive_frame is not None
    assert "ts" in alive_frame.payload
    assert isinstance(alive_frame.payload["ts"], float)
    # Plausible Unix timestamp (> 2023-ish)
    assert alive_frame.payload["ts"] > 1_700_000_000


def test_alive_ticker_stops_on_exit():
    """When the inner receives `exit` and shuts down cleanly, the
    alive thread must not prevent process termination — i.e. it must
    be a daemon thread. Verified by measuring clean exit time."""
    proc, _name = _spawn_inner()

    # Wait for first alive frame to confirm the thread is running
    deadline = time.time() + 25
    buf = ""
    try:
        while time.time() < deadline:
            chunk = _read_available_with_timeout(proc.stdout, 0.5)
            if chunk:
                buf += chunk
                if any(f.kind == "alive" for f in decode_all(buf)):
                    break
    except Exception:
        pass

    # Now shutdown and measure how long it takes
    start = time.time()
    try:
        proc.stdin.write("exit\n")
        proc.stdin.flush()
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("inner did not exit within 5s of receiving 'exit' — "
                    "alive thread is probably not a daemon")
    elapsed = time.time() - start
    # Clean shutdown should be near-instant, not "wait for next alive tick".
    assert elapsed < 3.0, (
        f"clean shutdown took {elapsed:.2f}s — alive thread may be blocking exit"
    )
