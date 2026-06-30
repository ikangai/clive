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


class TestShellZshFenceAndPrompt:
    """LLM-agnostic extraction: ```shell / ```zsh tags and '$ '/'# ' prompts.

    clive is model-agnostic; non-Claude models routinely emit ```shell or
    ```zsh fences, and shell blocks whose lines carry a leading '$ ' / '# '
    interactive-prompt prefix. The shell fence regex and prompt-stripping must
    handle these so a valid command isn't mis-extracted or dropped.
    """

    def test_fenced_shell_tag(self):
        reply = "Let me look.\n```shell\nls -la /tmp\n```\n"
        assert extract_command(reply) == "ls -la /tmp"

    def test_fenced_zsh_tag(self):
        reply = "```zsh\ngrep -r TODO .\n```"
        assert extract_command(reply) == "grep -r TODO ."

    def test_fenced_shell_multiline(self):
        reply = "```shell\nmkdir -p /tmp/out\ncp *.txt /tmp/out/\n```"
        assert extract_command(reply) == "mkdir -p /tmp/out\ncp *.txt /tmp/out/"

    def test_fenced_zsh_multiline(self):
        reply = "```zsh\ncd /tmp\nls\n```"
        assert extract_command(reply) == "cd /tmp\nls"

    def test_dollar_prompt_stripped_in_shell_block(self):
        reply = "```shell\n$ curl -s https://example.com\n```"
        assert extract_command(reply) == "curl -s https://example.com"

    def test_hash_root_prompt_stripped_in_zsh_block(self):
        reply = "```zsh\n# apt-get update\n```"
        assert extract_command(reply) == "apt-get update"

    def test_dollar_prompt_stripped_in_bash_block(self):
        reply = "```bash\n$ echo hello\n```"
        assert extract_command(reply) == "echo hello"

    def test_shell_block_preferred_over_bare(self):
        reply = "I suggest:\n```shell\nfind . -name '*.py'\n```\nAlternatively: ls"
        assert extract_command(reply) == "find . -name '*.py'"

    def test_command_with_hash_inside_not_a_leading_prompt(self):
        """Only a LEADING prompt is stripped; an inline '#' is left intact."""
        reply = "```shell\necho '# not a prompt'\n```"
        assert extract_command(reply) == "echo '# not a prompt'"

    def test_hash_comment_in_multiline_block_not_stripped(self):
        """A leading '# ...' in a MULTI-line block is a shell comment, not a
        root prompt — leave it so the shell ignores it and runs the command.
        ('$ ' has no such ambiguity and is stripped even when multi-line.)
        """
        reply = "```bash\n# build first\nmake\n```"
        assert extract_command(reply) == "# build first\nmake"

    def test_dollar_prompt_stripped_first_line_multiline(self):
        reply = "```shell\n$ cd /tmp\nls\n```"
        assert extract_command(reply) == "cd /tmp\nls"


class TestDoneInsideFenceIgnored:
    """DONE: lines inside fenced code blocks must NOT count as completion.

    Regression guard: a real command whose body/output contains a line
    starting 'DONE:' (heredoc, pasted scrollback, echoed text) was being
    mis-scored as task-complete, so the command never ran.
    """

    def test_done_inside_bash_block_ignored_by_extract_done(self):
        reply = "```bash\ncat <<'EOF'\nDONE: this is data, not a signal\nEOF\n```"
        assert extract_done(reply) is None

    def test_done_echoed_in_command_ignored(self):
        """A command line that emits 'DONE: ...' is data, not completion."""
        reply = "```bash\nDONE: leftover scrollback from a prior run\n```"
        assert extract_done(reply) is None

    def test_command_with_done_in_body_is_still_executed(self):
        """The actual command must run, not be swallowed as DONE."""
        reply = "Let me write the marker file.\n```bash\necho 'DONE: x' > /tmp/marker\n```"
        assert extract_done(reply) is None
        assert extract_command(reply) == "echo 'DONE: x' > /tmp/marker"

    def test_done_in_heredoc_body_command_executed(self):
        reply = "```bash\ncat <<'EOF' > log.txt\nDONE: not a real signal\nEOF\n```"
        assert extract_done(reply) is None
        assert extract_command(reply) == "cat <<'EOF' > log.txt\nDONE: not a real signal\nEOF"

    def test_done_in_no_lang_fence_ignored(self):
        reply = "Here is the prior output:\n```\nDONE: old run finished\n```"
        assert extract_done(reply) is None

    def test_genuine_top_level_done_still_detected(self):
        """A real DONE: outside any fence is still a completion signal."""
        reply = "DONE: wrote 3 files"
        assert extract_done(reply) == "wrote 3 files"
        assert extract_command(reply) is None

    def test_top_level_done_after_fenced_block_detected(self):
        """DONE: outside the fence still wins even when a fence is present."""
        reply = "```bash\necho 'DONE: noise'\n```\nDONE: real summary"
        assert extract_done(reply) == "real summary"
        assert extract_command(reply) is None
