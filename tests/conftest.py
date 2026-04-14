"""Pytest configuration — ensure src/clive/ is on sys.path for flat imports."""
import sys
import os

# Add src/clive/ to sys.path so flat imports like `from models import Subtask` work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "clive"))
