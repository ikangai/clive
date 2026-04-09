# tests/test_command_extract.py
"""Tests for plain-text command extraction (replaces XML parsing)."""
import pytest
from command_extract import extract_command, extract_done


class TestExtractDone:
    def test_done_at_start(self):
        assert extract_done("DONE: fetched 3 files") == "fetched 3 files"

    def test_done_after_text(self):
        reply = "All good.\nDONE: wrote results to output.csv"
        assert extract_done(reply) == "wrote results to output.csv"

    def test_no_done(self):
        assert extract_done("ls -la\nsome output") is None

    def test_done_empty(self):
        assert extract_done("DONE:") == ""

    def test_done_with_leading_space(self):
        assert extract_done("DONE:  trimmed") == "trimmed"


class TestExtractCommand:
    def test_fenced_bash(self):
        reply = "Let me check.\n```bash\nls -la /tmp\n```\n"
        assert extract_command(reply) == "ls -la /tmp"

    def test_fenced_sh(self):
        reply = "```sh\ngrep -r TODO .\n```"
        assert extract_command(reply) == "grep -r TODO ."

    def test_fenced_no_lang(self):
        reply = "```\ncat file.txt\n```"
        assert extract_command(reply) == "cat file.txt"

    def test_fenced_multiline(self):
        reply = "```bash\nmkdir -p /tmp/out\ncp *.txt /tmp/out/\n```"
        assert extract_command(reply) == "mkdir -p /tmp/out\ncp *.txt /tmp/out/"

    def test_dollar_prefix(self):
        reply = "Run this:\n$ curl -s https://example.com"
        assert extract_command(reply) == "curl -s https://example.com"

    def test_bare_command(self):
        reply = "ls -la /tmp/clive"
        assert extract_command(reply) == "ls -la /tmp/clive"

    def test_skip_comments(self):
        reply = "# This is a plan\nls /tmp"
        assert extract_command(reply) == "ls /tmp"

    def test_done_returns_none(self):
        reply = "DONE: all finished"
        assert extract_command(reply) is None

    def test_empty(self):
        assert extract_command("") is None

    def test_only_prose(self):
        # Prose-only reply -- no clear command
        reply = "I think we should check the logs first."
        cmd = extract_command(reply)
        # Should return the first line as best guess (LLM rarely does this)
        assert cmd is not None

    def test_fenced_python_ignored(self):
        """Python blocks are not shell commands -- skip them."""
        reply = "```python\nprint('hello')\n```"
        assert extract_command(reply) is None

    def test_fenced_bash_preferred_over_bare(self):
        reply = "I suggest:\n```bash\nfind . -name '*.py'\n```\nAlternatively: ls"
        assert extract_command(reply) == "find . -name '*.py'"

    def test_no_lang_block_with_python_content(self):
        """No-lang fenced block containing python should be rejected."""
        reply = "```\nimport json\njson.dumps({'a': 1})\n```"
        assert extract_command(reply) is None

    def test_python_then_bash_dual_blocks(self):
        """When reply has both python and bash blocks, extract bash."""
        reply = "```python\nprint('hello')\n```\nAlso:\n```bash\nls -la\n```"
        assert extract_command(reply) == "ls -la"

    def test_empty_fenced_block(self):
        """Empty fenced block returns empty string."""
        reply = "```bash\n\n```"
        assert extract_command(reply) == ""
