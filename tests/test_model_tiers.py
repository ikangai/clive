"""Tests for model tier resolution (resolve_model_tier)."""

import os
from unittest.mock import patch

from runtime import resolve_model_tier, _TIER_MAP


# --- Basic resolution ---

def test_fast_openai():
    assert resolve_model_tier("fast", "openai") == "gpt-4o-mini"


def test_fast_anthropic():
    assert resolve_model_tier("fast", "anthropic") == "claude-haiku-4-5-20251001"


def test_fast_gemini():
    assert resolve_model_tier("fast", "gemini") == "gemini-2.0-flash"


def test_fast_openrouter():
    assert resolve_model_tier("fast", "openrouter") == "google/gemini-2.0-flash-exp:free"


def test_fast_ollama():
    assert resolve_model_tier("fast", "ollama") == "llama3"


def test_fast_lmstudio():
    assert resolve_model_tier("fast", "lmstudio") == "local"


def test_fast_delegate():
    assert resolve_model_tier("fast", "delegate") is None


# --- "default" tier always returns None ---

def test_default_openai():
    assert resolve_model_tier("default", "openai") is None


def test_default_anthropic():
    assert resolve_model_tier("default", "anthropic") is None


def test_default_gemini():
    assert resolve_model_tier("default", "gemini") is None


# --- None tier always returns None ---

def test_none_tier():
    assert resolve_model_tier(None, "openai") is None


def test_none_tier_no_provider():
    assert resolve_model_tier(None) is None


# --- Unknown provider returns None ---

def test_fast_unknown_provider():
    assert resolve_model_tier("fast", "unknown_provider_xyz") is None


# --- Falls back to LLM_PROVIDER env when provider not given ---

def test_uses_llm_provider_env():
    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}):
        assert resolve_model_tier("fast") == "claude-haiku-4-5-20251001"


def test_uses_llm_provider_env_openai():
    with patch.dict(os.environ, {"LLM_PROVIDER": "openai"}):
        assert resolve_model_tier("fast") == "gpt-4o-mini"


def test_default_env_fallback():
    """When LLM_PROVIDER is not set, defaults to openrouter."""
    with patch.dict(os.environ, {}, clear=True):
        # Ensure LLM_PROVIDER is not set
        os.environ.pop("LLM_PROVIDER", None)
        assert resolve_model_tier("fast") == "google/gemini-2.0-flash-exp:free"


# --- All providers in _TIER_MAP have both tiers ---

def test_all_providers_have_fast_and_default():
    for provider, tiers in _TIER_MAP.items():
        assert "fast" in tiers, f"{provider} missing 'fast' tier"
        assert "default" in tiers, f"{provider} missing 'default' tier"
        assert tiers["default"] is None, f"{provider} 'default' should be None"
