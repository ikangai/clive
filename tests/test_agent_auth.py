# tests/test_agent_auth.py
import os
from server.auth import (
    generate_token, validate_token, load_agent_tokens,
    save_agent_token, AuthResult,
)

def test_generate_token():
    """Generated tokens must be non-empty strings."""
    token = generate_token()
    assert isinstance(token, str)
    assert len(token) >= 32

def test_validate_token_success():
    """Valid token must be accepted."""
    token = generate_token()
    result = validate_token(token, expected=token)
    assert result.allowed

def test_validate_token_failure():
    """Invalid token must be rejected."""
    result = validate_token("wrong-token", expected="correct-token")
    assert not result.allowed
    assert "invalid" in result.reason.lower() or "mismatch" in result.reason.lower()

def test_validate_token_empty():
    """Empty token must be rejected."""
    result = validate_token("", expected="some-token")
    assert not result.allowed

def test_validate_token_none_expected():
    """When no token is expected (auth disabled), any token passes."""
    result = validate_token("any-value", expected=None)
    assert result.allowed

def test_load_agent_tokens_missing_file():
    """Missing tokens file should return empty dict."""
    tokens = load_agent_tokens("/nonexistent/agents.yaml")
    assert tokens == {}

def test_save_and_load_token(tmp_path):
    """Saved token must be loadable."""
    path = str(tmp_path / "agents.yaml")
    save_agent_token(path, "testhost", "secret-token-123")
    tokens = load_agent_tokens(path)
    assert tokens.get("testhost") == "secret-token-123"

def test_save_multiple_tokens(tmp_path):
    """Multiple tokens for different hosts must coexist."""
    path = str(tmp_path / "agents.yaml")
    save_agent_token(path, "host-a", "token-a")
    save_agent_token(path, "host-b", "token-b")
    tokens = load_agent_tokens(path)
    assert tokens["host-a"] == "token-a"
    assert tokens["host-b"] == "token-b"

def test_auth_from_env(tmp_path):
    """CLIVE_AUTH_TOKEN env var must be usable for validation."""
    os.environ["CLIVE_AUTH_TOKEN"] = "env-token-xyz"
    try:
        result = validate_token(os.environ["CLIVE_AUTH_TOKEN"], expected="env-token-xyz")
        assert result.allowed
    finally:
        del os.environ["CLIVE_AUTH_TOKEN"]
