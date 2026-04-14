# command_extract.py
"""Plain-text command extraction from LLM replies.

Replaces the XML <cmd> protocol. The LLM just types what it would
type at a terminal. Commands are extracted from fenced code blocks
or bare lines.
"""
import re

_FENCED_SHELL_RE = re.compile(
    r'```(?:bash|sh)\s*\n(.*?)```', re.DOTALL
)
_FENCED_PYTHON_RE = re.compile(
    r'```python[3]?\s*\n.*?```', re.DOTALL
)
_DONE_RE = re.compile(r'^DONE:\s*(.*)', re.MULTILINE)

# Lines that are clearly not commands
_SKIP_PREFIXES = ('#', '//', 'DONE:', '> ')


def extract_done(reply: str) -> str | None:
    """Extract completion summary from DONE: marker. Returns None if not found."""
    m = _DONE_RE.search(reply)
    if m:
        return m.group(1).strip()
    return None


def extract_command(reply: str) -> str | None:
    """Extract shell command from LLM reply.

    Priority:
    1. DONE: marker -> return None (task complete, no command)
    2. Fenced ```bash or ```sh block -> return contents
    3. Line starting with $ -> return remainder
    4. First non-comment, non-empty line -> return as command
    """
    if not reply or not reply.strip():
        return None

    # 1. DONE signal -- no command to execute
    if extract_done(reply) is not None:
        return None

    # 2. Fenced ```bash or ```sh block (preferred — explicit shell)
    m = _FENCED_SHELL_RE.search(reply)
    if m:
        return m.group(1).strip()

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
