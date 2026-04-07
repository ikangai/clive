"""Tests for session-scoped filesystem."""
import re
from session import generate_session_id


def test_session_id_format():
    sid = generate_session_id()
    assert re.match(r"^[a-z0-9]{8}$", sid)


def test_session_id_unique():
    ids = {generate_session_id() for _ in range(100)}
    assert len(ids) == 100
