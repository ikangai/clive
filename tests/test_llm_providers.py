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
