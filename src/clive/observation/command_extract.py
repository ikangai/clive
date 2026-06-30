# command_extract.py
"""Plain-text command extraction from LLM replies.

Replaces the XML <cmd> protocol. The LLM just types what it would
type at a terminal. Commands are extracted from fenced code blocks
or bare lines.
"""
import re

_FENCED_SHELL_RE = re.compile(
    r'```(?:bash|sh|shell|zsh)\s*\n(.*?)```', re.DOTALL
)
_FENCED_PYTHON_RE = re.compile(
    r'```python[3]?\s*\n.*?```', re.DOTALL
)
_DONE_RE = re.compile(r'^DONE:\s*(.*)', re.MULTILINE)

# Fenced code blocks: a closed ```...``` pair, or a trailing unclosed fence
# (mid-stream / malformed). Their contents are command bodies or pasted
# output, never a top-level completion signal — excise them before the
# DONE: search so an echoed/heredoc/scrollback 'DONE:' can't be mis-scored.
_CLOSED_FENCE_RE = re.compile(r'```.*?```', re.DOTALL)
_OPEN_FENCE_RE = re.compile(r'```.*', re.DOTALL)

# Lines that are clearly not commands
_SKIP_PREFIXES = ('#', '//', 'DONE:', '> ')


def _strip_prompt(command: str) -> str:
    """Strip a leading interactive-shell prompt ('$ ' or '# ') from a command.

    Non-Claude models often paste commands carrying a prompt prefix (clive is
    model-agnostic). '$ ' (normal-user prompt) can never legitimately begin a
    shell command, so it is always stripped. '# ' is ambiguous — it is also a
    shell comment — so it is treated as a root prompt only for a single-line
    command; in a multi-line block a leading '# ...' is a real comment the shell
    will ignore on its own. Only the leading prompt is removed; an inline
    '$'/'#' (e.g. inside a quoted argument) is left intact.
    """
    if command.startswith('$ '):
        return command[2:]
    if command.startswith('# ') and '\n' not in command:
        return command[2:]
    return command


def _strip_fences(reply: str) -> str:
    """Remove fenced code blocks so their contents can't trigger DONE detection."""
    return _OPEN_FENCE_RE.sub('', _CLOSED_FENCE_RE.sub('', reply))


def extract_done(reply: str) -> str | None:
    """Extract completion summary from DONE: marker. Returns None if not found.

    DONE: must appear at top level — a DONE: line inside a fenced code block
    (command body, heredoc, or pasted scrollback) is data, not a signal.
    """
    m = _DONE_RE.search(_strip_fences(reply))
    if m:
        return m.group(1).strip()
    return None


def extract_command(reply: str) -> str | None:
    """Extract shell command from LLM reply.

    Priority:
    1. DONE: marker -> return None (task complete, no command)
    2. Fenced ```bash/```sh/```shell/```zsh block -> return contents
       (a leading '$ ' / '# ' shell prompt is stripped)
    3. Line starting with $ -> return remainder
    4. First non-comment, non-empty line -> return as command
    """
    if not reply or not reply.strip():
        return None

    # 1. DONE signal -- no command to execute
    if extract_done(reply) is not None:
        return None

    # 2. Fenced ```bash/```sh/```shell/```zsh block (preferred — explicit shell)
    m = _FENCED_SHELL_RE.search(reply)
    if m:
        return _strip_prompt(m.group(1).strip())

    # 2b. Skip non-shell fenced blocks (python, etc.)
    if _FENCED_PYTHON_RE.search(reply):
        return None

    # 2c. Fenced block with no language tag — accept if content doesn't look like python
    m = re.search(r'```\s*\n(.*?)```', reply, re.DOTALL)
    if m:
        content = m.group(1).strip()
        if content.startswith(('import ', 'from ', 'def ', 'class ', 'print(')):
            return None  # looks like python, reject entirely
        return content

    # 3. $ prefix
    for line in reply.splitlines():
        stripped = line.strip()
        if stripped.startswith('$ '):
            return stripped[2:]

    # 4. First non-skip line
    for line in reply.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if stripped.startswith('```'):
            continue
        return stripped

    return None
