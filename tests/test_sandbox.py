# tests/test_sandbox.py
import subprocess
import os

SANDBOX_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "src", "clive", "sandbox", "run.sh")


def test_sandbox_script_exists():
    assert os.path.isfile(SANDBOX_SCRIPT)


def test_sandbox_blocks_write_outside_workdir(tmp_path):
    """Sandbox must prevent writes outside the session directory.

    The escape target is under $HOME, not /tmp. This test used to probe
    /tmp and then accept ANY outcome on Darwin — but the macOS profile
    grants /tmp on purpose, so the probe could never have failed there
    and the Darwin branch made the whole assertion vacuous. $HOME is
    outside the profile's write set on macOS and outside the bwrap bind
    on Linux, so it is a real escape probe on both.
    """
    escape_file = os.path.join(
        os.path.expanduser("~"), f"clive_escape_probe_{os.getpid()}"
    )
    if os.path.exists(escape_file):
        os.unlink(escape_file)
    try:
        result = subprocess.run(
            ["bash", SANDBOX_SCRIPT, str(tmp_path), f"touch {escape_file}"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0 or not os.path.exists(escape_file), (
            f"command escaped the sandbox and wrote {escape_file}; "
            f"stderr={result.stderr!r}"
        )
    finally:
        if os.path.exists(escape_file):
            os.unlink(escape_file)


def test_sandbox_allows_write_inside_workdir(tmp_path):
    """Sandbox must allow writes inside the session directory."""
    result = subprocess.run(
        ["bash", SANDBOX_SCRIPT, str(tmp_path), f"touch {tmp_path}/inside_test"],
        capture_output=True, text=True, timeout=10,
    )
    assert os.path.exists(f"{tmp_path}/inside_test")


def test_sandbox_blocks_network_if_restricted(tmp_path):
    """If network=false in profile, outbound connections must fail."""
    result = subprocess.run(
        ["bash", SANDBOX_SCRIPT, str(tmp_path), "curl -s http://example.com",
         "--no-network"],
        capture_output=True, text=True, timeout=10,
    )
    # Asserted on every platform. The Darwin branch here used to accept
    # any outcome "because sandbox-exec may not work reliably" — it does
    # work; curl exits nonzero under (deny network*). Accepting anything
    # meant this test could not fail on the maintainer's own machine.
    assert result.returncode != 0, (
        f"--no-network did not block the request; stdout={result.stdout[:200]!r}"
    )


# ─── gh#55: no sandbox mechanism must refuse, not run unconfined ───────

def _fake_linux_path(tmp_path):
    """A PATH whose `uname` reports Linux and which has no bwrap.

    Reproduces the environment that exposed this: a Linux host (or CI
    runner) without bubblewrap installed, where run.sh previously fell
    through to a ulimit-only branch offering no isolation at all.
    """
    fake = tmp_path / "fakebin"
    fake.mkdir()
    uname = fake / "uname"
    uname.write_text("#!/bin/sh\necho Linux\n")
    uname.chmod(0o755)
    return f"{fake}:/usr/bin:/bin"


def test_sandbox_refuses_when_no_mechanism_available(tmp_path):
    """Silently running unconfined hands the caller a false assurance.

    Callers sandbox precisely because they are about to run an
    LLM-generated command, so 'no mechanism' must mean 'refuse'.
    """
    workdir = tmp_path / "wd"
    workdir.mkdir()
    escape = tmp_path / "escaped"
    env = dict(os.environ, PATH=_fake_linux_path(tmp_path))
    env.pop("CLIVE_SANDBOX_ALLOW_UNCONFINED", None)

    result = subprocess.run(
        ["bash", SANDBOX_SCRIPT, str(workdir), f"touch {escape}"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert result.returncode != 0, "must refuse rather than run unconfined"
    assert not escape.exists(), "command ran despite no sandbox being available"
    assert "refusing to run" in result.stderr


def test_sandbox_unconfined_escape_hatch_is_explicit(tmp_path):
    """The operator can opt out, but must say so and gets warned."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    env = dict(os.environ, PATH=_fake_linux_path(tmp_path),
               CLIVE_SANDBOX_ALLOW_UNCONFINED="1")

    result = subprocess.run(
        ["bash", SANDBOX_SCRIPT, str(workdir), "true"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert "UNCONFINED" in result.stderr
    assert "NOT enforced" in result.stderr
