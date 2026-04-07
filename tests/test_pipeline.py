"""Tests for command pipelining — multiple commands per LLM response."""
from executor import parse_commands


def test_single_command():
    text = '<cmd type="shell" pane="eval">ls -la</cmd>'
    cmds = parse_commands(text)
    assert len(cmds) == 1
    assert cmds[0]["type"] == "shell"


def test_multiple_shell_commands():
    text = '''I'll do this in steps:

<cmd type="shell" pane="eval">mkdir -p /tmp/clive/data</cmd>
<cmd type="shell" pane="eval">echo '{"name": "alice"}' > /tmp/clive/data/result.json</cmd>
<cmd type="task_complete">Created data directory and wrote result file</cmd>
'''
    cmds = parse_commands(text)
    assert len(cmds) == 3
    assert cmds[0]["type"] == "shell"
    assert cmds[1]["type"] == "shell"
    assert cmds[2]["type"] == "task_complete"


def test_mixed_command_types():
    text = '''<cmd type="shell" pane="eval">curl -s api.example.com > /tmp/clive/api.json</cmd>
<cmd type="read_file" pane="eval">/tmp/clive/api.json</cmd>
<cmd type="task_complete">Fetched and read API response</cmd>'''
    cmds = parse_commands(text)
    assert len(cmds) == 3
    assert cmds[0]["type"] == "shell"
    assert cmds[1]["type"] == "read_file"
    assert cmds[2]["type"] == "task_complete"


def test_no_command_returns_none():
    text = "I need to think about this approach..."
    cmds = parse_commands(text)
    assert len(cmds) == 1
    assert cmds[0]["type"] == "none"


def test_pipeline_preserves_order():
    text = '<cmd type="shell" pane="e">first</cmd><cmd type="shell" pane="e">second</cmd>'
    cmds = parse_commands(text)
    assert cmds[0]["value"] == "first"
    assert cmds[1]["value"] == "second"
