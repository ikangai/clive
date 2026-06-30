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

    ``with_exit=None`` consults :func:`ps1_exit_enabled`. The exit form
    branches on ``$ZSH_VERSION`` so the *same* line works in either shell:

    - bash: ``PROMPT_COMMAND`` captures ``$?`` before each render and PS1
      expands ``${__clive_ec}`` (bash ``promptvars`` is on by default).
    - zsh: ``PROMPT_COMMAND`` is ignored and ``${var}`` is not expanded in
      the prompt unless ``PROMPT_SUBST`` is set — so use ``setopt
      PROMPT_SUBST`` + a ``precmd()`` hook + ``PROMPT`` (gh#8 follow-up;
      without this, a zsh pane on macOS rendered the literal
      ``${__clive_ec}`` and broke prompt detection).

    Both branches render the identical ``[AGENT_READY] ec=<n> $`` sentinel,
    so :data:`PS1_EXIT_RE` and completion detection stay shell-agnostic.
    Each branch parses in both shells; only the matching one runs.
    """
    if with_exit is None:
        with_exit = ps1_exit_enabled()
    if not with_exit:
        return f'export PS1="{PLAIN_PS1}"'
    prompt = "[AGENT_READY] ec=${__clive_ec} $ "
    zsh = "setopt PROMPT_SUBST; precmd() { __clive_ec=$?; }; PROMPT='" + prompt + "'"
    bash = "export PROMPT_COMMAND='__clive_ec=$?'; export PS1='" + prompt + "'"
    return 'if [ -n "$ZSH_VERSION" ]; then ' + zsh + '; else ' + bash + '; fi'


def pager_safe_env_setup() -> str:
    """Return the export line that disables pagers/editors for a fresh pane.

    Pager avoidance is otherwise only *advisory* (drivers/default.md and
    ``llm.prompts`` tell the model to pipe pager-y output through ``| cat`` and
    use ``git --no-pager``). Any common command that invokes a pager — ``git
    log``/``diff``/``branch``, ``man``, ``systemctl status``, ``docker logs`` —
    opens an interactive pager that wedges the pane; the streaming/event path
    doesn't detect it at all and the poll path only flags it as an
    intervention, forcing the agent to escape it and burning turns.

    Sent deterministically at pane setup (alongside the PS1 install), this is
    the environment-level backstop that realizes clive's "shell where judgment
    isn't required" principle:

    - ``PAGER``/``GIT_PAGER``/``MANPAGER=cat`` route pager output straight to
      the pane, and ``LESS=-FRX`` makes any stray ``less`` quit on a short page
      instead of waiting for a keypress.
    - ``EDITOR=true`` makes commands that would drop into ``$EDITOR`` (e.g. a
      missing ``-m`` on ``git commit``) no-op instead of opening ``vi``.
    - ``GIT_TERMINAL_PROMPT=0`` fails git auth prompts fast rather than
      blocking the pane on interactive input.
    - ``DEBIAN_FRONTEND=noninteractive``/``PIP_NO_INPUT=1``/``NONINTERACTIVE=1``
      stop the package managers from blocking the pane on an interactive prompt
      — apt's "Configuring tzdata" dialog (the most common autonomous-agent
      wedge), pip keyring prompts, and Homebrew confirmations. All three are
      well-established and side-effect-free for non-interactive use. ``CI=1`` is
      deliberately *not* set: it changes tool behavior, not just prompting.

    A single side-effect-free ``export`` line so it composes with the existing
    ``send_keys`` setup sequence.
    """
    return (
        "export PAGER=cat GIT_PAGER=cat MANPAGER=cat "
        "EDITOR=true GIT_TERMINAL_PROMPT=0 LESS=-FRX "
        "DEBIAN_FRONTEND=noninteractive PIP_NO_INPUT=1 NONINTERACTIVE=1"
    )


def parse_ps1_exit(line: str | None) -> int | None:
    """Extract the exit code from a rendered exit-bearing prompt line.

    Returns ``None`` for a plain prompt, the un-rendered setup echo (which
    contains the literal ``${__clive_ec}``, not digits), or empty input.
    """
    m = PS1_EXIT_RE.search(line or "")
    return int(m.group(1)) if m else None
