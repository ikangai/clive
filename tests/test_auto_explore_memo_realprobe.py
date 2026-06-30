"""Tests for _derive_memo_fields preferring a real-usage probe over --help (gh#41).

``build_exploration_goal`` instructs the explorer agent to run ``<tool> --help``
first, so the FIRST successful probe is almost always a help/version invocation.
Recording that as the learned ``invocation`` teaches the next run to re-run
``jq --help`` instead of a real usage like ``jq -r '.x'`` — defeating the
experiential-reuse goal. ``_derive_memo_fields`` must therefore prefer the first
successful probe that is NOT help/version-only, falling back to the first
success only when every success is help-only (so it never regresses to the empty
"nothing learned" invocation that bohr's gh#41 guard depends on).

These are direct unit tests on ``_derive_memo_fields`` — no LLM/tmux/filesystem.
"""
from discovery.auto import _derive_memo_fields
from discovery.explorer import _HELP_FLAGS
from discovery.models import ExplorationResult, ProbeOutcome


def _result(*probes):
    r = ExplorationResult(tool_name="jq")
    for cmd, code in probes:
        r.probes.append(ProbeOutcome(command=cmd, exit_code=code, screen=""))
    return r


# ── (1) real-usage success after an earlier help-only success wins ────────────

def test_prefers_real_usage_over_earlier_help_success():
    # The --help probe succeeds first (as build_exploration_goal instructs),
    # then a real usage succeeds. The real usage must be the learned invocation.
    result = _result(("jq --help", 0), ("jq -r '.x'", 0))

    invocation, _usage = _derive_memo_fields("jq", result, "jq: a JSON processor\n")

    assert invocation == "jq -r '.x'"


def test_skips_help_only_even_when_real_usage_comes_much_later():
    # A failed probe, a help success, a version success, then finally real usage.
    result = _result(
        ("jq --bogus", 2),
        ("jq --help", 0),
        ("jq --version", 0),
        ("jq -c '.[]'", 0),
    )

    invocation, _usage = _derive_memo_fields("jq", result, "synopsis\n")

    assert invocation == "jq -c '.[]'"


# ── (2) all-help-only successes fall back to the first success (never empty) ───

def test_falls_back_to_first_success_when_all_help_only():
    # Every success is help/version-only — must fall back to the first success,
    # NOT regress to "" (which would defeat bohr's empty-invocation guard and
    # downgrade a prior good memo on re-exploration).
    result = _result(("jq --help", 0), ("jq --version", 0))

    invocation, _usage = _derive_memo_fields("jq", result, "synopsis\n")

    assert invocation == "jq --help"


def test_no_successful_probe_yields_empty_invocation():
    # No exit_code==0 probe — the empty "nothing learned" signal is preserved
    # so record_tool_memo no-ops and a prior good memo survives (gh#41).
    result = _result(("jq --help", 1), ("jq -r '.x'", 2))

    invocation, _usage = _derive_memo_fields("jq", result, "synopsis\n")

    assert invocation == ""


# ── (3) a bare tool-name success is a real usage, not help-only ───────────────

def test_bare_tool_name_success_is_real_usage():
    # A command that is just the tool name (no flags) is a legitimate real
    # invocation (cf. record_tool_memo("fzf", "fzf", ...)), so a later help-only
    # success must not be preferred over it.
    result = _result(("jq", 0), ("jq --help", 0))

    invocation, _usage = _derive_memo_fields("jq", result, "synopsis\n")

    assert invocation == "jq"


# ── (4) sanity: the imported flag set is the explorer's, shared not copied ─────

def test_uses_explorer_help_flags():
    assert "--help" in _HELP_FLAGS and "--version" in _HELP_FLAGS
