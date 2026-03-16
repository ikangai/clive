"""Tests for driver prompt auto-discovery."""
import os
import tempfile
from prompts import load_driver


def test_load_existing_driver(tmp_path):
    driver_file = tmp_path / "shell.md"
    driver_file.write_text("# Shell driver\nKEYS: ctrl-c=interrupt")
    result = load_driver("shell", drivers_dir=str(tmp_path))
    assert "Shell driver" in result
    assert "ctrl-c=interrupt" in result


def test_load_missing_driver_returns_default(tmp_path):
    result = load_driver("nonexistent_tool", drivers_dir=str(tmp_path))
    assert result  # should return the default driver, not empty
    assert "autonomous agent" in result.lower() or "worker" in result.lower() or "control" in result.lower()


def test_load_driver_from_real_drivers_dir():
    """Once we create drivers/default.md, this should find it via fallback."""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    drivers_dir = os.path.join(project_dir, "drivers")
    # Test fallback — no driver for 'nonexistent' should return default
    result = load_driver("nonexistent", drivers_dir=drivers_dir)
    assert len(result) > 10
