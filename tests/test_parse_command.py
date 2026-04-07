"""Tests for command parsing from LLM responses."""
from executor import parse_command


def test_parse_shell_command():
    text = '<cmd type="shell" pane="main">ls -la /tmp</cmd>'
    cmd = parse_command(text)
    assert cmd["type"] == "shell"
    assert cmd["pane"] == "main"
    assert cmd["value"] == "ls -la /tmp"


def test_parse_task_complete():
    text = '<cmd type="task_complete">Done: found 3 files</cmd>'
    cmd = parse_command(text)
    assert cmd["type"] == "task_complete"
    assert cmd["pane"] is None
    assert "found 3 files" in cmd["value"]


def test_parse_read_file():
    text = '<cmd type="read_file" pane="shell">/tmp/data.txt</cmd>'
    cmd = parse_command(text)
    assert cmd["type"] == "read_file"
    assert cmd["value"] == "/tmp/data.txt"


def test_parse_write_file_pane_first():
    text = '<cmd type="write_file" pane="shell" path="/tmp/out.txt">hello world</cmd>'
    cmd = parse_command(text)
    assert cmd["type"] == "write_file"
    assert cmd["pane"] == "shell"
    assert cmd["path"] == "/tmp/out.txt"
    assert cmd["value"] == "hello world"


def test_parse_write_file_path_first():
    text = '<cmd type="write_file" path="/tmp/out.txt" pane="shell">content here</cmd>'
    cmd = parse_command(text)
    assert cmd["type"] == "write_file"
    assert cmd["pane"] == "shell"
    assert cmd["path"] == "/tmp/out.txt"
    assert cmd["value"] == "content here"


def test_parse_no_command():
    text = "I need to think about this..."
    cmd = parse_command(text)
    assert cmd["type"] == "none"
    assert cmd["value"] == ""


def test_parse_command_with_surrounding_text():
    text = "Let me list the files.\n\n<cmd type=\"shell\" pane=\"main\">ls -la</cmd>\n\nThis should work."
    cmd = parse_command(text)
    assert cmd["type"] == "shell"
    assert cmd["value"] == "ls -la"


def test_parse_multiline_write():
    text = '''<cmd type="write_file" pane="shell" path="/tmp/script.sh">#!/bin/bash
echo "hello"
echo "world"</cmd>'''
    cmd = parse_command(text)
    assert cmd["type"] == "write_file"
    assert "#!/bin/bash" in cmd["value"]
    assert 'echo "world"' in cmd["value"]


def test_parse_single_quotes():
    text = "<cmd type='shell' pane='main'>echo 'hello'</cmd>"
    cmd = parse_command(text)
    assert cmd["type"] == "shell"
    assert cmd["value"] == "echo 'hello'"


def test_parse_command_without_pane():
    text = '<cmd type="shell">ls</cmd>'
    cmd = parse_command(text)
    assert cmd["type"] == "shell"
    assert cmd["pane"] is None
    assert cmd["value"] == "ls"
