"""Tests for per-pane model selection (Strategy 4 foundation)."""

import types
from unittest.mock import MagicMock

from models import PaneInfo
from prompts import _parse_driver_frontmatter, load_driver_meta, _driver_cache, _driver_meta_cache


def _make_pane_info(**kwargs):
    """Create a PaneInfo with a mock tmux pane."""
    defaults = dict(
        pane=MagicMock(),
        app_type="shell",
        description="test pane",
        name="shell",
    )
    defaults.update(kwargs)
    return PaneInfo(**defaults)


# --- PaneInfo field tests ---

def test_pane_info_model_fields_default_none():
    pi = _make_pane_info()
    assert pi.agent_model is None
    assert pi.observation_model is None


def test_pane_info_model_fields_set():
    pi = _make_pane_info(
        agent_model="claude-haiku-4-5-20251001",
        observation_model="gemini-2.0-flash",
    )
    assert pi.agent_model == "claude-haiku-4-5-20251001"
    assert pi.observation_model == "gemini-2.0-flash"


def test_pane_info_existing_fields_preserved():
    """Ensure adding model fields doesn't break existing fields."""
    pi = _make_pane_info(
        idle_timeout=5.0,
        sandboxed=True,
        frame_nonce="abc123",
        agent_model="test-model",
    )
    assert pi.idle_timeout == 5.0
    assert pi.sandboxed is True
    assert pi.frame_nonce == "abc123"
    assert pi.agent_model == "test-model"


# --- Frontmatter parsing tests ---

def test_frontmatter_parses_model_keys():
    content = """---
preferred_mode: script
agent_model: claude-haiku-4-5-20251001
observation_model: gemini-2.0-flash
---
Driver body here."""
    body, meta = _parse_driver_frontmatter(content)
    assert meta["agent_model"] == "claude-haiku-4-5-20251001"
    assert meta["observation_model"] == "gemini-2.0-flash"
    assert meta["preferred_mode"] == "script"
    assert body == "Driver body here."


def test_frontmatter_missing_model_keys():
    content = """---
preferred_mode: interactive
---
Driver body."""
    body, meta = _parse_driver_frontmatter(content)
    assert "agent_model" not in meta
    assert "observation_model" not in meta


def test_frontmatter_no_frontmatter():
    content = "Just a plain driver."
    body, meta = _parse_driver_frontmatter(content)
    assert meta == {}
    assert body == content


# --- Fallback behaviour tests ---

def test_fallback_agent_model_or_global():
    """When agent_model is None, fallback expression returns the global."""
    pi = _make_pane_info(agent_model=None)
    global_model = "claude-sonnet-4-20250514"
    effective = pi.agent_model or global_model
    assert effective == global_model


def test_override_agent_model():
    """When agent_model is set, it takes precedence over global."""
    pi = _make_pane_info(agent_model="claude-haiku-4-5-20251001")
    global_model = "claude-sonnet-4-20250514"
    effective = pi.agent_model or global_model
    assert effective == "claude-haiku-4-5-20251001"


def test_fallback_observation_model():
    pi = _make_pane_info(observation_model=None)
    global_model = "claude-sonnet-4-20250514"
    effective = pi.observation_model or global_model
    assert effective == global_model


def test_override_observation_model():
    pi = _make_pane_info(observation_model="gemini-2.0-flash")
    global_model = "claude-sonnet-4-20250514"
    effective = pi.observation_model or global_model
    assert effective == "gemini-2.0-flash"


# --- load_driver_meta integration test ---

def test_load_driver_meta_returns_model_keys(tmp_path):
    """load_driver_meta should return agent_model/observation_model from frontmatter."""
    driver_file = tmp_path / "custom.md"
    driver_file.write_text("""---
preferred_mode: script
agent_model: gpt-4o-mini
observation_model: gemini-2.0-flash
---
Custom driver instructions.""")

    # Clear caches so our temp driver is loaded
    cache_key = f"custom:{tmp_path}"
    _driver_cache.pop(cache_key, None)
    _driver_meta_cache.pop(cache_key, None)

    meta = load_driver_meta("custom", drivers_dir=str(tmp_path))
    assert meta["agent_model"] == "gpt-4o-mini"
    assert meta["observation_model"] == "gemini-2.0-flash"

    # Clean up caches
    _driver_cache.pop(cache_key, None)
    _driver_meta_cache.pop(cache_key, None)


def test_load_driver_meta_no_model_keys(tmp_path):
    """Driver without model keys should return empty for those keys."""
    driver_file = tmp_path / "plain.md"
    driver_file.write_text("""---
preferred_mode: interactive
---
Plain driver.""")

    cache_key = f"plain:{tmp_path}"
    _driver_cache.pop(cache_key, None)
    _driver_meta_cache.pop(cache_key, None)

    meta = load_driver_meta("plain", drivers_dir=str(tmp_path))
    assert meta.get("agent_model") is None
    assert meta.get("observation_model") is None

    _driver_cache.pop(cache_key, None)
    _driver_meta_cache.pop(cache_key, None)
