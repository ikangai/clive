"""Pytest configuration — ensure src/clive/ is on sys.path for flat imports."""
import sys
import os

_here = os.path.dirname(__file__)
# Add repo root FIRST so `from evals.harness...` resolves (evals/ lives there),
# then src/clive/ so flat imports like `from models import Subtask` take
# priority over any shadowing packages at repo root (e.g. the clive.py
# wrapper — tests want src/clive/clive.py, not the root wrapper).
sys.path.insert(0, os.path.join(_here, ".."))
sys.path.insert(0, os.path.join(_here, "..", "src", "clive"))


def pytest_configure(config):
    """Register custom markers used in the test suite."""
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow (real tmux / network / multi-second); opt-in via -m slow",
    )
