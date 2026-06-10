"""End-to-end CLI test for ``--join room@lobby``.

Spawns a real ``clive --role broker`` subprocess as the lobby, then
a second ``clive --name alice --conversational --join general@testlobby``
subprocess that should connect, send bootstrap frames, and stay
alive until we feed it ``exit`` on stdin.

We verify the bootstrap traffic landed in the lobby by connecting a
raw third socket and listing the room roster after alice joined.
The LLM is not invoked in this test (no thread is opened), so we
don't need to mock it — the test exercises the connector + bootstrap
path, which is the part the library-level tests cannot touch.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from protocol import decode_all, encode


REPO_ROOT = Path(__file__).resolve().parent.parent
CLIVE_CMD = [sys.executable, str(REPO_ROOT / "clive.py")]


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Isolate ~/.clive so this test cannot see or clobber a user's
    real registry. We point HOME at tmp_path so subprocesses inherit
    the isolation via their own env."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return {
        "home": home,
        "registry_dir": home / ".clive" / "instances",
    }


# Cold `clive` subprocesses (full interpreter + app import) plus broker
# event-loop startup can take several seconds when the whole test suite is
# running in parallel. The old fixed 3s waits + a single 0.5s member-bootstrap
# sleep raced under that load — whichever socket test got starved failed.
# Make the waits generous and env-tunable, and poll for room membership
# instead of assuming it after a fixed sleep.
_ROOMS_TIMEOUT = float(os.environ.get("CLIVE_TEST_ROOMS_TIMEOUT", "15"))


def _wait_for(predicate, *, timeout=_ROOMS_TIMEOUT, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _observer_confirms_member_in_room(sock_path, room, members, *,
                                      timeout=_ROOMS_TIMEOUT):
    """Open an observer socket, join `room`, and open a thread including
    `members`; the broker returns `thread_opened` iff every member is
    currently in-room, else `nack:members_not_in_room`.

    A member's join can lag behind its process start under load, so this
    polls — reconnecting and retrying open_thread until `thread_opened` or
    the overall timeout — rather than assuming membership after a fixed
    sleep. Returns the frame kinds seen on the final attempt.
    """
    deadline = time.time() + timeout
    last_kinds: set = set()
    while time.time() < deadline:
        obs = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            obs.connect(sock_path)
            obs.sendall(b"NONCE nO\n")
            obs.sendall((encode("session_hello",
                                {"client_kind": "clive", "name": "observer"},
                                nonce="nO") + "\n").encode())
            obs.settimeout(2.0)
            obs.recv(4096)   # session_ack
            obs.sendall((encode("join_room", {"room": room},
                                nonce="nO") + "\n").encode())
            time.sleep(0.1)
            obs.sendall((encode("open_thread", {
                "room": room,
                "members": list(members),
                "private": False,
                "prompt": "ping",
            }, nonce="nO") + "\n").encode())

            kinds: set = set()
            inner = time.time() + 2.0
            data = b""
            while (time.time() < inner and "thread_opened" not in kinds
                   and "nack" not in kinds):
                try:
                    chunk = obs.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
                for f in decode_all(data.decode("utf-8", errors="replace"),
                                    nonce="nO"):
                    kinds.add(f.kind)
            last_kinds = kinds
            if "thread_opened" in kinds:
                return kinds
        finally:
            obs.close()
        # Member join may still be in flight (nack) — back off and retry.
        time.sleep(0.2)
    return last_kinds


def test_join_without_name_is_rejected(tmp_env):
    """`--join` silently no-oped without `--name` in earlier revisions
    because the rooms wire-up lives inside the conversational
    keep-alive branch, which is gated on `--name`. Now the argparse
    layer surfaces the requirement with exit code 2 + a helpful
    stderr message instead of letting the flag do nothing."""
    proc = subprocess.run(
        CLIVE_CMD + ["--conversational", "--join", "general@lobby"],
        env={**os.environ, "HOME": str(tmp_env["home"])},
        capture_output=True, timeout=10,
    )
    assert proc.returncode == 2, (
        f"expected exit 2, got {proc.returncode}; "
        f"stderr={proc.stderr.decode()[:400]}"
    )
    assert b"--join requires --name" in proc.stderr


def test_join_auto_enables_conversational(tmp_env):
    """`--join --name alice` must auto-enable conversational keep-alive
    — otherwise the rooms wire-up is skipped and --join silently
    does nothing. We prove this by launching alice without
    `--conversational` and verifying she still bootstraps onto the
    lobby (observer's open_thread listing alice succeeds iff alice
    joined)."""
    sock_path = tempfile.mkdtemp(prefix="clv-cli-") + "/lobby.sock"
    broker = subprocess.Popen(
        CLIVE_CMD + [
            "--role", "broker",
            "--name", "testlobby",
            "--lobby-socket", sock_path,
        ],
        env={**os.environ, "HOME": str(tmp_env["home"])},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for(lambda: os.path.exists(sock_path)), \
            "lobby socket never appeared"
        assert _wait_for(
            lambda: (tmp_env["registry_dir"] / "testlobby.json").exists()
        ), "lobby registry entry never appeared"

        # Deliberately omit `--conversational`. Previously this meant
        # alice entered REPL mode instead of the keep-alive loop,
        # and --join was ignored.
        member = subprocess.Popen(
            CLIVE_CMD + [
                "--name", "alice",
                "--join", "general@testlobby",
            ],
            env={**os.environ, "HOME": str(tmp_env["home"])},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            # Poll for alice in-room rather than assuming a fixed sleep is
            # enough — her join can lag her process start under load.
            kinds = _observer_confirms_member_in_room(
                sock_path, "general", ["observer", "alice"])
            assert "thread_opened" in kinds, (
                f"alice's --join did not take effect without explicit "
                f"--conversational; observer saw: {sorted(kinds)}"
            )
        finally:
            try:
                member.stdin.write(b"exit\n")
                member.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                member.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                member.kill()
                member.wait(timeout=1.0)
    finally:
        broker.terminate()
        try:
            broker.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.wait(timeout=1.0)


def test_cli_join_flag_bootstraps_against_running_lobby(tmp_env):
    """Full CLI path end-to-end. The lobby subprocess runs, registers,
    and accepts. The member subprocess parses --join, resolves the
    lobby name via the local registry, opens a Unix socket, completes
    the NONCE handshake, and sends session_hello + join_room.

    We verify the roster by opening a third socket as `observer`,
    joining the same room, and opening a thread including alice — the
    open_thread will be nacked with `members_not_in_room` if alice
    did NOT actually join, so a green thread_opened proves the full
    CLI path worked."""
    sock_path = tempfile.mkdtemp(prefix="clv-cli-") + "/lobby.sock"

    # Start the lobby.
    broker = subprocess.Popen(
        CLIVE_CMD + [
            "--role", "broker",
            "--name", "testlobby",
            "--lobby-socket", sock_path,
        ],
        env={**os.environ, "HOME": str(tmp_env["home"])},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        # Wait for the broker's socket to appear AND its registry
        # entry to be written — both are set up in start() before
        # run_forever() enters the event loop.
        assert _wait_for(lambda: os.path.exists(sock_path)), \
            f"lobby socket never appeared at {sock_path}"
        assert _wait_for(
            lambda: (tmp_env["registry_dir"] / "testlobby.json").exists()
        ), "lobby registry entry never appeared"

        # Start the member.
        member = subprocess.Popen(
            CLIVE_CMD + [
                "--name", "alice",
                "--conversational",
                "--join", "general@testlobby",
            ],
            env={**os.environ, "HOME": str(tmp_env["home"])},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            # Detect completion by polling the lobby as an observer and
            # verifying alice is in-room — reconnect+retry until she has
            # joined (open_thread nacks members_not_in_room until then),
            # which is robust to her join lagging under load.
            kinds = _observer_confirms_member_in_room(
                sock_path, "general", ["observer", "alice"])
            assert "thread_opened" in kinds, (
                f"alice's --join did not place her in room `general`; "
                f"observer's open_thread saw kinds: {sorted(kinds)}"
            )
        finally:
            try:
                member.stdin.write(b"exit\n")
                member.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                member.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                member.kill()
                member.wait(timeout=1.0)
    finally:
        broker.terminate()
        try:
            broker.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.wait(timeout=1.0)
