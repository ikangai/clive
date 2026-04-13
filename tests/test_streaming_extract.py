# tests/test_streaming_extract.py
"""Tests for streaming command extraction."""


def test_detector_fires_on_complete_bash_block():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("I'll list the files.\n")
    assert commands == []
    d.feed("I'll list the files.\n```bash\n")
    assert commands == []
    d.feed("I'll list the files.\n```bash\nls -la\n")
    assert commands == []
    d.feed("I'll list the files.\n```bash\nls -la\n```")
    assert commands == ["ls -la"]


def test_detector_fires_once():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("```bash\nls\n```\nNow let me explain...")
    d.feed("```bash\nls\n```\nNow let me explain what I did.")
    assert len(commands) == 1


def test_detector_ignores_python_blocks():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("```python\nprint('hi')\n```")
    assert commands == []


def test_detector_returns_done_signal():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("DONE: task complete")
    assert commands == []
    assert d.done_detected


def test_detector_no_command():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("I think we should wait and see what happens next.")
    assert commands == []
    assert not d.done_detected


def test_detector_multiline_command():
    from streaming_extract import StreamingCommandDetector
    commands = []
    d = StreamingCommandDetector(on_command=lambda cmd: commands.append(cmd))
    d.feed("```bash\nfind /tmp \\\n  -name '*.log' \\\n  -delete\n```")
    assert len(commands) == 1
    assert "find /tmp" in commands[0]
    assert "-delete" in commands[0]
