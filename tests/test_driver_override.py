"""Tests for driver prompt override."""
import os
from prompts import load_driver


def test_driver_override_via_env(tmp_path, monkeypatch):
    """CLIVE_EVAL_DRIVER_OVERRIDE should override any driver."""
    override_file = tmp_path / "custom_shell.md"
    override_file.write_text("CUSTOM OVERRIDE DRIVER")
    monkeypatch.setenv("CLIVE_EVAL_DRIVER_OVERRIDE", str(override_file))
    result = load_driver("shell")
    assert result == "CUSTOM OVERRIDE DRIVER"


def test_driver_override_not_set():
    """Without env var, load_driver behaves normally."""
    os.environ.pop("CLIVE_EVAL_DRIVER_OVERRIDE", None)
    result = load_driver("shell")
    assert "Shell Driver" in result or "shell" in result.lower()
    assert len(result) > 50


def test_driver_override_missing_file(monkeypatch):
    """If override file doesn't exist, fall back to normal behavior."""
    monkeypatch.setenv("CLIVE_EVAL_DRIVER_OVERRIDE", "/nonexistent/driver.md")
    result = load_driver("shell")
    assert len(result) > 50
