"""Pytest configuration — ensure src/clive/ is on sys.path for flat imports."""
import sys
import os

# Add src/clive/ to sys.path so flat imports like `from models import Subtask` work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "clive"))


def pytest_configure(config):
    """Register custom markers used in the test suite."""
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow (real tmux / network / multi-second); opt-in via -m slow",
    )
