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
    assert llm.PROVIDER_NAME == "delegate"
