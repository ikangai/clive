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


def _wait_for(predicate, *, timeout=3.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


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
            # Give the member time to connect + bootstrap. We
            # detect completion by polling the lobby as an observer
            # and verifying alice is in-room.
            time.sleep(0.5)   # cooperative: member connects, sends hello+join

            # Observer: connect raw, send hello + join_room(general),
            # then open_thread with alice — only succeeds if alice is
            # a current room member.
            obs = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            obs.connect(sock_path)
            obs.sendall(b"NONCE nO\n")
            obs.sendall((encode("session_hello",
                                {"client_kind": "clive", "name": "observer"},
                                nonce="nO") + "\n").encode())
            obs.settimeout(1.0)
            obs.recv(4096)   # session_ack
            obs.sendall((encode("join_room", {"room": "general"},
                                nonce="nO") + "\n").encode())
            time.sleep(0.1)
            obs.sendall((encode("open_thread", {
                "room": "general",
                "members": ["observer", "alice"],
                "private": False,
                "prompt": "ping",
            }, nonce="nO") + "\n").encode())

            # Read until we either see `thread_opened` (alice IS
            # in-room) or `nack:members_not_in_room` (she isn't).
            obs.settimeout(2.0)
            data = b""
            kinds = set()
            deadline = time.time() + 3.0
            while time.time() < deadline and "thread_opened" not in kinds \
                    and "nack" not in kinds:
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

            obs.close()
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
