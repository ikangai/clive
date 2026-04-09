# tests/test_discovery.py
from server.discovery import discover_sessions, generate_unique_session_name


def test_discover_sessions_returns_list():
    """discover_sessions must return a list (may be empty)."""
    sessions = discover_sessions()
    assert isinstance(sessions, list)


def test_generate_unique_name_differs():
    """Two calls must produce different names."""
    name1 = generate_unique_session_name()
    name2 = generate_unique_session_name()
    assert name1 != name2


def test_generate_unique_name_has_prefix():
    """Generated names must start with 'clive-'."""
    name = generate_unique_session_name()
    assert name.startswith("clive-")


def test_discover_session_format():
    """Each discovered session must have expected fields."""
    sessions = discover_sessions()
    for s in sessions:
        assert "name" in s
        assert "panes" in s
