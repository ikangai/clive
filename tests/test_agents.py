"""Tests for agent addressing and resolution."""
import os
import tempfile

import pytest

from agents import parse_agent_addresses, resolve_agent, build_agent_ssh_cmd


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """Make every test in this file hermetic w.r.t. ~/.clive.

    resolve_agent() calls ensure_ssh_control_dir() to guarantee the
    SSH pooling directory exists before handing out a pane_def. That
    side effect would otherwise litter the developer's real home on
    every test run — the exact pollution Phase 3.5 M2 removed from
    build_agent_ssh_cmd. We pin HOME to a per-module tmp dir so the
    side effect lands somewhere disposable.

    Tests that want a specific HOME (e.g. ensure_ssh_control_dir
    tests with a ~/.clive file) re-monkeypatch HOME themselves.
    """
    home = tmp_path_factory.mktemp("fake-home")
    monkeypatch.setenv("HOME", str(home))
    yield


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


# ─── Local-provider → delegate auto-override ─────────────────────────────────

def test_local_provider_forces_delegate(monkeypatch):
    """When the outer is on LMStudio (localhost-only), the remote
    cannot reach it without tunneling. build_agent_ssh_cmd must
    override LLM_PROVIDER=delegate on the remote so the inner routes
    inference back through the conversational channel."""
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    cmd = build_agent_ssh_cmd("prod.example.com", config={})
    assert "LLM_PROVIDER=delegate" in cmd
    assert "AGENT_MODEL=delegate" in cmd
    assert "--conversational" in cmd


def test_ollama_also_forces_delegate(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    cmd = build_agent_ssh_cmd("prod.example.com", config={})
    assert "LLM_PROVIDER=delegate" in cmd


def test_cloud_provider_does_not_force_delegate(monkeypatch):
    """Cloud providers (Anthropic, OpenAI, OpenRouter, Gemini) are
    reachable from the remote directly — forward the env vars via
    SendEnv and let the remote call the cloud endpoint itself. No
    delegate override."""
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    cmd = build_agent_ssh_cmd("prod.example.com", config={})
    assert "SendEnv=LLM_PROVIDER" in cmd
    assert "SendEnv=OPENROUTER_API_KEY" in cmd
    assert "LLM_PROVIDER=delegate" not in cmd


def test_anthropic_does_not_force_delegate(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    cmd = build_agent_ssh_cmd("prod.example.com", config={})
    assert "LLM_PROVIDER=delegate" not in cmd
    assert "SendEnv=ANTHROPIC_API_KEY" in cmd


def test_delegate_override_appears_before_clive_command(monkeypatch):
    """The LLM_PROVIDER=delegate assignment must sit in the remote
    command string BEFORE the clive.py invocation so the env var is
    visible when the inner reads it at startup."""
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    cmd = build_agent_ssh_cmd("host", config={})
    # The remote command is quoted; find the opening quote and check ordering
    idx_provider = cmd.find("LLM_PROVIDER=delegate")
    idx_clive = cmd.find("clive.py")
    assert idx_provider != -1 and idx_clive != -1
    assert idx_provider < idx_clive


# ─── LLM_BASE_URL / GOOGLE_API_KEY forwarding ────────────────────────────────

def test_google_api_key_is_forwarded(monkeypatch):
    """Gemini support — GOOGLE_API_KEY must be in the forwarded env list."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-fake")
    cmd = build_agent_ssh_cmd("host", config={})
    assert "SendEnv=GOOGLE_API_KEY" in cmd


def test_llm_base_url_is_forwarded(monkeypatch):
    """When the outer uses LLM_BASE_URL to point at a proxy, the
    remote should see the same base url so it can reach the same
    endpoint (assuming network reachability)."""
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_BASE_URL", "http://proxy:8080/v1")
    cmd = build_agent_ssh_cmd("host", config={})
    assert "SendEnv=LLM_BASE_URL" in cmd


# ─── SSH ControlMaster connection pooling ────────────────────────────────────

def test_ssh_cmd_enables_controlmaster_when_ctl_dir_exists(tmp_path, monkeypatch):
    """Agent panes open many rapid SSH connections (delegate round
    trips, scp for file transfer, reconnects). ControlMaster pools
    them over a single SSH channel so handshakes don't dominate
    latency. The ControlMaster options are emitted only when the
    control socket dir exists on disk — if it's missing (clive not
    yet run, or ensure_ssh_control_dir failed), we degrade to
    unpooled SSH instead of pointing tmux at a nonexistent path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    # Pre-create the control dir — startup hook's job in production,
    # the test does it explicitly here.
    (tmp_path / ".clive" / "ssh").mkdir(parents=True, mode=0o700)
    cmd = build_agent_ssh_cmd("host", config={})
    assert "ControlMaster=auto" in cmd
    assert "ControlPath=" in cmd
    assert "ControlPersist=" in cmd


def test_ssh_cmd_controlpath_uses_clive_ssh_dir(tmp_path, monkeypatch):
    """Control sockets live under ~/.clive/ssh/ so they are isolated
    from the user's normal SSH control sockets and cleaned up by a
    single rm -rf if something wedges."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".clive" / "ssh").mkdir(parents=True, mode=0o700)
    cmd = build_agent_ssh_cmd("host", config={})
    assert ".clive/ssh" in cmd


def test_ssh_cmd_skips_controlmaster_when_dir_missing(tmp_path, monkeypatch):
    """Degraded mode: if ~/.clive/ssh does not exist, build_agent_ssh_cmd
    must NOT emit ControlMaster options (pointing ssh at a nonexistent
    ControlPath would log a warning per connection). Clive works
    without pooling, just slower."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Deliberately do NOT create .clive/ssh
    cmd = build_agent_ssh_cmd("host", config={})
    assert "ControlMaster=auto" not in cmd
    assert "ControlPath=" not in cmd
    assert "ControlPersist=" not in cmd


def test_build_agent_ssh_cmd_has_no_filesystem_side_effects(tmp_path, monkeypatch):
    """Regression test for M2.

    Previously, build_agent_ssh_cmd called os.makedirs on ~/.clive/ssh
    as a side effect of building the SSH command string. Every test
    invocation (and there are 20+) left the directory behind in the
    user's real home. Pure string-builder functions must stay pure;
    directory creation now lives in a startup hook in clive.py via
    ensure_ssh_control_dir.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    from agents import build_agent_ssh_cmd
    build_agent_ssh_cmd("host", config={})
    assert not (tmp_path / ".clive" / "ssh").exists(), (
        "build_agent_ssh_cmd still creates ~/.clive/ssh as a side "
        "effect — the directory creation should live in a startup "
        "hook, not in this string-builder."
    )


def test_ensure_ssh_control_dir_creates_dir(tmp_path, monkeypatch):
    """The startup helper that replaces the side effect must exist
    and create the dir idempotently with mode 0o700."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from agents import ensure_ssh_control_dir
    p = ensure_ssh_control_dir()
    assert (tmp_path / ".clive" / "ssh").is_dir()
    # Second call is a no-op (idempotent) and returns the same path.
    p2 = ensure_ssh_control_dir()
    assert p == p2


def test_ensure_ssh_control_dir_degrades_when_parent_is_file(tmp_path, monkeypatch):
    """If ~/.clive exists as a regular file (edge case, e.g. user's
    dotfile symlinked wrong), ensure_ssh_control_dir must not crash —
    SSH pooling is an optimization, not a correctness feature, so
    degrade gracefully."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Create ~/.clive as a FILE, not a directory
    (tmp_path / ".clive").write_text("accidentally a file")
    from agents import ensure_ssh_control_dir
    # Must not raise; returns None to signal "no dir available".
    result = ensure_ssh_control_dir()
    assert result is None


def test_resolve_agent_ensures_ssh_control_dir(tmp_path, monkeypatch):
    """Regression test for L-A.

    Phase 3.5 moved os.makedirs out of build_agent_ssh_cmd and into
    clive.py's __main__ startup hook. That worked for the clive.py
    entry point but left a latent bug: tui.py (and any future entry
    that skips clive.py's __main__) never called ensure_ssh_control_dir,
    so the first agent-pane resolution from those paths ran with
    ControlMaster disabled.

    The fix is to call ensure_ssh_control_dir() lazily from
    resolve_agent() — idempotent, cheap on subsequent calls, and
    covers every entry point that can reach agent addressing.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not (tmp_path / ".clive" / "ssh").exists()
    from agents import resolve_agent
    pane_def = resolve_agent("someagent")
    # The side effect: resolve_agent created the control dir
    assert (tmp_path / ".clive" / "ssh").is_dir()
    # And because the dir now exists, build_agent_ssh_cmd emitted
    # the ControlMaster options.
    assert "ControlMaster=auto" in pane_def["cmd"]
