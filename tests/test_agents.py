"""Tests for agent addressing and resolution."""
import os
import tempfile
from agents import parse_agent_addresses, resolve_agent, build_agent_ssh_cmd


# ─── Address parsing ─────────────────────────────────────────────────────────

def test_parse_single_address():
    result = parse_agent_addresses("ask clive@devbox to check disk usage")
    assert len(result) == 1
    assert result[0] == ("devbox", "ask to check disk usage")


def test_parse_address_at_start():
    result = parse_agent_addresses("clive@localhost read HN")
    assert result[0] == ("localhost", "read HN")


def test_parse_no_address():
    result = parse_agent_addresses("check disk usage")
    assert result == []


def test_parse_multiple_addresses():
    result = parse_agent_addresses(
        "ask clive@gpu to render video then clive@web to upload it"
    )
    assert len(result) == 2
    hosts = [r[0] for r in result]
    assert "gpu" in hosts
    assert "web" in hosts


def test_parse_address_with_dots():
    result = parse_agent_addresses("clive@prod.example.com check health")
    assert result[0][0] == "prod.example.com"


def test_parse_address_with_hyphens():
    result = parse_agent_addresses("clive@my-server check health")
    assert result[0][0] == "my-server"


# ─── Resolution ──────────────────────────────────────────────────────────────

def test_resolve_auto():
    """Auto-resolve without registry returns default SSH pane def."""
    pane_def = resolve_agent("myhost")
    assert pane_def["name"] == "agent-myhost"
    assert pane_def["app_type"] == "agent"
    assert pane_def["host"] == "myhost"
    assert "ssh" in pane_def["cmd"]
    assert "myhost" in pane_def["cmd"]
    assert "--conversational" in pane_def["cmd"]


def test_resolve_from_registry():
    """Registry entry overrides auto-resolve."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("devbox:\n  host: devbox.local\n  toolset: web\n  path: /opt/clive/clive.py\n")
        f.flush()
        try:
            pane_def = resolve_agent("devbox", registry_path=f.name)
            assert pane_def["host"] == "devbox.local"
            assert "-t web" in pane_def["cmd"]
            assert "/opt/clive/clive.py" in pane_def["cmd"]
        finally:
            os.unlink(f.name)


def test_resolve_registry_with_key():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("secure:\n  key: ~/.ssh/special_key\n")
        f.flush()
        try:
            pane_def = resolve_agent("secure", registry_path=f.name)
            assert "-i ~/.ssh/special_key" in pane_def["cmd"]
        finally:
            os.unlink(f.name)


def test_resolve_registry_missing_host_defaults_to_name():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("localhost:\n  toolset: web\n")
        f.flush()
        try:
            pane_def = resolve_agent("localhost", registry_path=f.name)
            assert pane_def["host"] == "localhost"
        finally:
            os.unlink(f.name)


# ─── SSH command building ────────────────────────────────────────────────────

def test_build_ssh_cmd_basic():
    cmd = build_agent_ssh_cmd("myhost", {})
    assert cmd.startswith("ssh ")
    assert "myhost" in cmd
    assert "-t" not in cmd  # no TTY allocation
    assert "--conversational" in cmd


def test_build_ssh_cmd_with_key():
    cmd = build_agent_ssh_cmd("myhost", {"key": "~/.ssh/mykey"})
    assert "-i ~/.ssh/mykey" in cmd


def test_build_ssh_cmd_with_toolset():
    cmd = build_agent_ssh_cmd("myhost", {"toolset": "web"})
    assert "-t web" in cmd


def test_build_ssh_cmd_with_custom_path():
    cmd = build_agent_ssh_cmd("myhost", {"path": "/opt/clive/clive.py"})
    assert "/opt/clive/clive.py" in cmd


def test_build_ssh_cmd_forwards_env():
    """SSH command should include SendEnv for API keys."""
    # Set a test env var to verify it gets forwarded
    old = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        cmd = build_agent_ssh_cmd("myhost", {})
        assert "SendEnv=ANTHROPIC_API_KEY" in cmd
    finally:
        if old:
            os.environ["ANTHROPIC_API_KEY"] = old
        else:
            del os.environ["ANTHROPIC_API_KEY"]


# ─── Session nonce injection ─────────────────────────────────────────────────

def test_build_ssh_cmd_injects_frame_nonce():
    """SSH command should set CLIVE_FRAME_NONCE to a fresh random value.

    The nonce is embedded as a remote-side env assignment so the inner's
    encode() picks it up via os.environ — no reliance on SendEnv, which
    would need sshd AcceptEnv configuration.
    """
    cmd = build_agent_ssh_cmd("myhost", {})
    assert "CLIVE_FRAME_NONCE=" in cmd
    # Extract the nonce value from the remote command
    import re
    m = re.search(r"CLIVE_FRAME_NONCE=([A-Za-z0-9_-]+)", cmd)
    assert m is not None
    nonce = m.group(1)
    # A 128-bit urlsafe nonce is ~22 characters
    assert len(nonce) >= 20


def test_resolve_agent_exposes_nonce_on_pane_def():
    """The pane_def returned by resolve_agent must carry the same nonce
    that was injected into the SSH command, so downstream parsers can
    use it to authenticate frames from this specific inner."""
    pane_def = resolve_agent("somehost")
    assert "frame_nonce" in pane_def
    assert pane_def["frame_nonce"] in pane_def["cmd"]


def test_build_ssh_cmd_nonces_are_unique_per_call():
    """Each SSH invocation must have a fresh nonce — reusing nonces
    across instances would let an attacker replay frames from a
    compromised inner into a sibling's pane."""
    import re
    c1 = build_agent_ssh_cmd("h1", {})
    c2 = build_agent_ssh_cmd("h1", {})
    n1 = re.search(r"CLIVE_FRAME_NONCE=([A-Za-z0-9_-]+)", c1).group(1)
    n2 = re.search(r"CLIVE_FRAME_NONCE=([A-Za-z0-9_-]+)", c2).group(1)
    assert n1 != n2
