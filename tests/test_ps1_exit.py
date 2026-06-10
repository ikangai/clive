"""Tests for exit-code-in-PS1 completion mechanism (gh#8).

Opt-in (CLIVE_PS1_EXITCODE=1) alternative to the EXIT:/___DONE___ command
wrapper: the shell prompt itself carries the last command's exit code, so
completion can be read from the prompt line without wrapping the command.
The default (flag off) path is unchanged — wrap_command stays authoritative.
"""
import pytest


# ─── prompt setup string ────────────────────────────────────────────────

def test_plain_setup_is_unchanged_ps1():
    from ps1_exit import agent_ready_prompt_setup
    # Flag off → byte-identical to the historical export, so existing
    # detection / health checks see no change.
    assert agent_ready_prompt_setup(with_exit=False) == 'export PS1="[AGENT_READY] $ "'


def test_exit_setup_captures_exit_via_prompt_command():
    from ps1_exit import agent_ready_prompt_setup
    setup = agent_ready_prompt_setup(with_exit=True)
    # bash branch: capture $? before the prompt renders, embed it in PS1.
    assert "PROMPT_COMMAND='__clive_ec=$?'" in setup
    assert "${__clive_ec}" in setup


def test_exit_setup_has_zsh_branch():
    """zsh ignores PROMPT_COMMAND and needs PROMPT_SUBST + precmd() for
    ${var} expansion in the prompt (gh#8 follow-up). The setup must branch
    on $ZSH_VERSION so the same line works in either shell."""
    from ps1_exit import agent_ready_prompt_setup
    setup = agent_ready_prompt_setup(with_exit=True)
    assert "ZSH_VERSION" in setup
    assert "setopt PROMPT_SUBST" in setup
    assert "precmd()" in setup
    assert "PROMPT=" in setup   # zsh prompt var (distinct from bash PS1=)


def test_exit_setup_has_both_shell_branches():
    from ps1_exit import agent_ready_prompt_setup
    setup = agent_ready_prompt_setup(with_exit=True)
    # bash branch
    assert "export PS1=" in setup
    assert "PROMPT_COMMAND" in setup
    # zsh branch
    assert "PROMPT=" in setup
    assert "precmd" in setup
    # single if/else line
    assert setup.startswith("if ") and setup.rstrip().endswith("fi")


def test_exit_setup_same_rendered_prompt_both_shells():
    """Both branches must render the identical sentinel so PS1_EXIT_RE and
    completion detection are shell-agnostic."""
    from ps1_exit import agent_ready_prompt_setup, PS1_EXIT_RE
    setup = agent_ready_prompt_setup(with_exit=True)
    # The literal ${__clive_ec} sentinel appears once per branch, both with
    # the [AGENT_READY] ec=... $ shape.
    assert setup.count("[AGENT_READY] ec=${__clive_ec} $") == 2
    # And the rendered form (digit substituted) is what the detector matches.
    assert PS1_EXIT_RE.search("[AGENT_READY] ec=0 $") is not None


def test_exit_ps1_preserves_agent_ready_substring():
    from ps1_exit import agent_ready_prompt_setup
    # check_health and plain-prompt detection both test for the literal
    # "[AGENT_READY]" substring — the exit form must keep it intact.
    setup = agent_ready_prompt_setup(with_exit=True)
    assert "[AGENT_READY]" in setup


def test_setup_consults_env_flag(monkeypatch):
    from ps1_exit import agent_ready_prompt_setup
    monkeypatch.setenv("CLIVE_PS1_EXITCODE", "1")
    assert "__clive_ec" in agent_ready_prompt_setup()  # with_exit=None → env
    monkeypatch.setenv("CLIVE_PS1_EXITCODE", "0")
    assert agent_ready_prompt_setup() == 'export PS1="[AGENT_READY] $ "'


def test_enabled_helper(monkeypatch):
    from ps1_exit import ps1_exit_enabled
    monkeypatch.delenv("CLIVE_PS1_EXITCODE", raising=False)
    assert ps1_exit_enabled() is False
    monkeypatch.setenv("CLIVE_PS1_EXITCODE", "1")
    assert ps1_exit_enabled() is True


# ─── parsing the rendered prompt ────────────────────────────────────────

def test_parse_exit_from_rendered_prompt_zero():
    from ps1_exit import parse_ps1_exit
    assert parse_ps1_exit("[AGENT_READY] ec=0 $ ") == 0


def test_parse_exit_from_rendered_prompt_nonzero():
    from ps1_exit import parse_ps1_exit
    assert parse_ps1_exit("/tmp/work [AGENT_READY] ec=127 $ ") == 127


def test_parse_returns_none_on_plain_prompt():
    from ps1_exit import parse_ps1_exit
    assert parse_ps1_exit("[AGENT_READY] $ ") is None


def test_parse_returns_none_on_unrendered_setup_echo():
    from ps1_exit import parse_ps1_exit
    # The export-command echo contains the literal ${__clive_ec}, not a
    # number — must NOT be mistaken for a completion with some exit code.
    echo = "export PROMPT_COMMAND='__clive_ec=$?'; export PS1='[AGENT_READY] ec=${__clive_ec} $ '"
    assert parse_ps1_exit(echo) is None


def test_parse_handles_empty_and_none():
    from ps1_exit import parse_ps1_exit
    assert parse_ps1_exit("") is None
    assert parse_ps1_exit(None) is None


def test_exit_regex_matches_only_digit_codes():
    from ps1_exit import PS1_EXIT_RE
    assert PS1_EXIT_RE.search("[AGENT_READY] ec=42 $") is not None
    assert PS1_EXIT_RE.search("[AGENT_READY] ec=$? $") is None


# ─── completion detection fires on the exit-bearing prompt (prod path) ──

def _fake_pane(screen_content: str):
    from unittest.mock import MagicMock
    pane = MagicMock()
    pane.cmd.return_value.stdout = screen_content.splitlines()
    return pane


def test_wait_for_ready_detects_exit_bearing_prompt():
    """The production poll path must recognize the gh#8 prompt as 'prompt'."""
    from completion import wait_for_ready
    from models import PaneInfo
    pane = _fake_pane("some output\n[AGENT_READY] ec=0 $ ")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell")
    screen, method = wait_for_ready(info, marker=None, max_wait=2.0)
    assert method == "prompt"


def test_wait_for_ready_still_detects_plain_prompt():
    """Default (flag-off) plain prompt detection is unchanged."""
    from completion import wait_for_ready
    from models import PaneInfo
    pane = _fake_pane("some output\n[AGENT_READY] $ ")
    info = PaneInfo(pane=pane, app_type="shell", description="", name="shell")
    screen, method = wait_for_ready(info, marker=None, max_wait=2.0)
    assert method == "prompt"
