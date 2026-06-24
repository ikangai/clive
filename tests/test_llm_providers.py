"""Tests for the LLM provider registry in llm.py."""


def test_delegate_provider_registered():
    from llm import PROVIDERS
    assert "delegate" in PROVIDERS
    cfg = PROVIDERS["delegate"]
    assert cfg["base_url"] is None         # no HTTP
    assert cfg["api_key_env"] is None      # no key needed — outer pays
    assert cfg["default_model"] == "delegate"


def test_delegate_provider_selectable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "delegate")
    import importlib, llm
    importlib.reload(llm)
    try:
        assert llm.PROVIDER_NAME == "delegate"
    finally:
        # Reset so later tests that plain-import llm see fresh module
        # state instead of a stale delegate client cached here.
        llm._client_cache = None
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        importlib.reload(llm)


def test_reload_cleanup_leaves_fresh_state():
    """Regression test for H1.

    Previously, tests that called importlib.reload(llm) with
    LLM_PROVIDER=delegate would leave llm._client_cache pointing at a
    DelegateClient and llm.PROVIDER_NAME stuck at "delegate" — later
    tests that plain-imported llm saw the stale state. Now the reload
    tests reset in a try/finally. This test runs AFTER both of them
    (same test file for test_delegate_provider_selectable; the
    test_delegate_client.py one runs earlier alphabetically) and
    asserts the cleanup held.
    """
    import llm
    assert llm._client_cache is None, (
        f"stale _client_cache leaked: {llm._client_cache!r}"
    )
    assert llm.PROVIDER_NAME != "delegate", (
        f"stale PROVIDER_NAME leaked: {llm.PROVIDER_NAME!r}"
    )


# ─── LLM_BASE_URL override ──────────────────────────────────────────────────

def test_llm_base_url_overrides_provider_default(monkeypatch):
    """A user running a local proxy / self-hosted endpoint should be
    able to point at it without editing llm.py. LLM_BASE_URL, when
    set, takes precedence over the provider's built-in base_url."""
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_BASE_URL", "http://my-proxy:8080/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
    import importlib, llm
    importlib.reload(llm)
    try:
        client = llm.get_client()
        # openai SDK exposes .base_url on the client
        assert str(client.base_url).startswith("http://my-proxy:8080")
    finally:
        llm._client_cache = None
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        importlib.reload(llm)


def test_llm_base_url_overrides_anthropic_default(monkeypatch):
    """Regression test for M1.

    Previously, the anthropic branch of get_client() constructed
    anthropic.Anthropic(api_key=...) without forwarding LLM_BASE_URL —
    users running a local Claude proxy (e.g. for logging, rate
    limiting, or self-hosted models through an anthropic-compatible
    gateway) would find their requests silently hitting
    api.anthropic.com instead of their proxy.

    The anthropic SDK does accept base_url as a constructor
    parameter; we now thread LLM_BASE_URL through to it.
    """
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_BASE_URL", "http://my-claude-proxy:7070")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    import importlib, llm
    importlib.reload(llm)
    try:
        client = llm.get_client()
        assert str(client.base_url).startswith("http://my-claude-proxy:7070"), (
            f"anthropic client ignored LLM_BASE_URL; got {client.base_url!r}"
        )
    finally:
        llm._client_cache = None
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(llm)


# ─── claude-cli panel isolation ─────────────────────────────────────────────
# The claude-cli provider shells out to `claude -p`. Because `claude` reads the
# operator's ~/.claude, an UN-isolated call becomes a full Claude Code agent:
# it loads user/project plugins (the group-chat plugin → registers a teammate
# handle, reads + POSTS to the live chat), runs SessionStart/Stop hooks (incl. a
# 600s team barrier), and connects every configured MCP server. As a *panel*
# (a dumb completion engine that drives clive, which orchestrates tools itself)
# it must have NONE of that surface. These tests pin the isolation.
#
# Mechanism is empirically chosen (verified against claude v2.1.187):
#   --setting-sources ""  drops user+project settings → `enabledPlugins` is never
#                         read → the plugin + its hooks never load. Crucially this
#                         is NOT --bare: subscription/keychain auth STILL works
#                         (--bare ignores both the keychain AND CLAUDE_CODE_OAUTH_TOKEN,
#                         leaving only ANTHROPIC_API_KEY = API spend — which we avoid).

def test_claude_cli_argv_is_fully_isolated():
    """The panel invocation must carry every isolation flag while preserving
    subscription auth: --setting-sources "" (no plugins/hooks, but keychain still
    works), zero tools, zero MCP — and NOT --bare (which would break keychain)."""
    import json
    import llm
    argv = llm._build_claude_cli_argv(model="sonnet")
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    # drop all setting sources → enabledPlugins not read → no plugin, no hooks.
    assert argv[argv.index("--setting-sources") + 1] == ""
    # zero tools: the panel returns assistant text only — never runs Bash/MCP/etc.
    assert argv[argv.index("--tools") + 1] == ""
    # ignore ALL ambient MCP config and load an empty server set.
    assert "--strict-mcp-config" in argv
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])
    assert mcp == {"mcpServers": {}}
    # the requested model is still forwarded.
    assert argv[argv.index("--model") + 1] == "sonnet"
    # --bare would disable keychain auth (subscription) — must NOT be used.
    assert "--bare" not in argv


def test_claude_cli_argv_omits_model_when_default():
    """No --model for Claude Code's default, but isolation flags stay."""
    import llm
    argv = llm._build_claude_cli_argv(model=None)
    assert "--model" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--bare" not in argv


def test_claude_cli_env_points_home_at_real_config_for_keychain_auth():
    """The isolated panel authenticates via the macOS login keychain, which lives
    under the REAL home. clive runs the candidate under HOME=sandbox, so the env
    builder must repoint HOME at the real home (from CLIVE_CLAUDECLI_HOME) for the
    `claude -p` subprocess. This is SAFE now: the agent surface is disabled by the
    argv flags (--setting-sources ""/--tools ""/--mcp-config), not by withholding
    HOME — so the real home no longer drags in plugins or the group chat."""
    import llm
    env = llm._build_claude_cli_env({"CLIVE_CLAUDECLI_HOME": "/Users/real",
                                     "HOME": "/tmp/cf-sandbox"})
    assert env["HOME"] == "/Users/real"


def test_claude_cli_env_leaves_home_when_no_real_home_given():
    """With no CLIVE_CLAUDECLI_HOME (e.g. running outside the factory sandbox),
    HOME is left untouched — the default config dir already has the credential."""
    import llm
    env = llm._build_claude_cli_env({"HOME": "/Users/real"})
    assert env["HOME"] == "/Users/real"
