"""Tests for command echo, auto-verification, and semantic signals."""
import json
import os
from executor import _detect_outcome_signal, _auto_verify_command


# ─── Semantic outcome detection ───────────────────────────────────────────────

def test_detect_error_signal():
    screen = "$ curl api.example.com\nerror: connection refused\n[AGENT_READY] $"
    assert "error" in _detect_outcome_signal(screen)


def test_detect_success_signal():
    screen = "$ echo data > out.txt\nSuccessfully written\n[AGENT_READY] $"
    assert "success" in _detect_outcome_signal(screen)


def test_no_signal_on_neutral_output():
    screen = "$ ls -la\nfile1.txt\nfile2.txt\n[AGENT_READY] $"
    assert _detect_outcome_signal(screen) == ""


def test_error_takes_precedence():
    screen = "error: not found\n[AGENT_READY] $"
    signal = _detect_outcome_signal(screen)
    assert "error" in signal


# ─── Auto-verification ────────────────────────────────────────────────────────

def test_auto_verify_file_write(tmp_path):
    # Create a file that the command would have written
    target = tmp_path / "result.txt"
    target.write_text("hello world")
    result = _auto_verify_command(f"echo hello > {target}", str(tmp_path))
    assert "exists" in result
    assert "11 bytes" in result


def test_auto_verify_json_file(tmp_path):
    target = tmp_path / "data.json"
    target.write_text(json.dumps({"key": "value"}))
    result = _auto_verify_command(f"jq . input > {target}", str(tmp_path))
    assert "valid JSON" in result


def test_auto_verify_no_redirect():
    result = _auto_verify_command("ls -la", "/tmp")
    assert result == ""


def test_auto_verify_missing_file(tmp_path):
    result = _auto_verify_command(f"echo x > {tmp_path}/nonexistent.txt", str(tmp_path))
    assert result == ""


# ─── Multi-language script extraction ─────────────────────────────────────────

def test_extract_python_script():
    from executor import _extract_script
    text = '''```python
#!/usr/bin/env python3
import json
data = [1, 2, 3]
print(json.dumps(data))
```'''
    script = _extract_script(text)
    assert script.startswith("#!/usr/bin/env python3")
    assert "import json" in script


def test_extract_bash_script():
    from executor import _extract_script
    text = '''```bash
#!/bin/bash
set -e
echo "hello"
```'''
    script = _extract_script(text)
    assert script.startswith("#!/bin/bash")
