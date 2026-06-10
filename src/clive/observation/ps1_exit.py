"""Exit-code-in-PS1 completion mechanism (gh#8).

Opt-in via ``CLIVE_PS1_EXITCODE=1`` (default off). Bakes the last command's
exit code into the shell prompt sentinel so completion detection can read
status from the prompt line itself, rather than depending on the appended
``EXIT:``/``___DONE___`` command wrapper
(``observation.completion.wrap_command``) — which stays the default and is
left byte-for-byte unchanged. Tradeoff (per the gh#8 card): couples to the
shell's prompt config.

The exit-bearing prompt deliberately keeps the literal ``[AGENT_READY]``
substring so the existing ``session.check_health`` and plain-prompt
detection still match; it inserts `` ec=<n>`` before the trailing ``$``.

This module ships the *mechanism* (prompt setup + parser). Re-wiring the
runners to drop ``wrap_command`` in favour of the PS1 exit code is a
deferred follow-up — see the gh#8 card note.
"""
from __future__ import annotations

import os
import re

PS1_EXIT_ENABLED_ENV = "CLIVE_PS1_EXITCODE"

# The historical prompt (flag off). Kept identical to session.py's literal.
PLAIN_PS1 = "[AGENT_READY] $ "

# Rendered exit-bearing prompt looks like:  [AGENT_READY] ec=0 $
# Search (not match) so a leading cwd/garbage on the line is tolerated.
PS1_EXIT_RE = re.compile(r"\[AGENT_READY\] ec=(\d+) \$")


def ps1_exit_enabled() -> bool:
    """True when the opt-in env flag is set."""
    return os.environ.get(PS1_EXIT_ENABLED_ENV) == "1"


def agent_ready_prompt_setup(with_exit: bool | None = None) -> str:
    """Return the shell command(s) to install the agent-ready prompt.

    ``with_exit=None`` consults :func:`ps1_exit_enabled`. With the exit form,
    ``PROMPT_COMMAND`` captures ``$?`` before each prompt render and PS1
    expands ``${__clive_ec}`` at render time (bash ``promptvars`` is on by
    default), yielding ``[AGENT_READY] ec=<n> $``.
    """
    if with_exit is None:
        with_exit = ps1_exit_enabled()
    if with_exit:
        return ("export PROMPT_COMMAND='__clive_ec=$?'; "
                "export PS1='[AGENT_READY] ec=${__clive_ec} $ '")
    return f'export PS1="{PLAIN_PS1}"'


def parse_ps1_exit(line: str | None) -> int | None:
    """Extract the exit code from a rendered exit-bearing prompt line.

    Returns ``None`` for a plain prompt, the un-rendered setup echo (which
    contains the literal ``${__clive_ec}``, not digits), or empty input.
    """
    m = PS1_EXIT_RE.search(line or "")
    return int(m.group(1)) if m else None
