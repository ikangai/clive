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
