"""Tests for driver prompt auto-discovery."""
import os
import tempfile
from prompts import load_driver, DEFAULT_DRIVER


# ─── Verify-before-done — the default driver must instruct the agent to ─────
# confirm the expected end-state actually holds before reporting success,
# rather than assuming the last command's exit code means the goal was met.

def test_default_driver_instructs_verify_before_done():
    """load_driver('default') must carry a verify-before-done step."""
    import prompts as prompts_mod
    prompts_mod._driver_cache.clear()
    prompts_mod._driver_meta_cache.clear()
    body = load_driver("default").lower()
    assert "verify" in body
    assert "before" in body and "done" in body
    # It must call out NOT trusting the exit code alone.
    assert "exit code" in body


def test_default_driver_constant_instructs_verify_before_done():
    """The DEFAULT_DRIVER fallback (unknown app_types) must match."""
    low = DEFAULT_DRIVER.lower()
    assert "verify" in low
    assert "before" in low and "done" in low
    assert "exit code" in low


# ─── Bounded recovery — the default driver must give concrete, bounded ──────
# recovery guidance instead of the vague "try a different approach": detect
# stuck/hung commands and interrupt them, cap retries at a small fixed number
# then stop and report, and prefer non-interactive invocations that never
# block waiting on a pager or prompt.

def test_default_driver_instructs_bounded_recovery():
    """load_driver('default') must carry concrete, bounded recovery steps."""
    import prompts as prompts_mod
    prompts_mod._driver_cache.clear()
    prompts_mod._driver_meta_cache.clear()
    body = load_driver("default").lower()
    # (1) Detect a stuck/hung command and interrupt rather than wait forever.
    assert "stuck" in body or "hung" in body
    assert "ctrl-c" in body or "interrupt" in body
    # (2) Bound retries to a small fixed number, then stop and report.
    assert "retr" in body  # retry / retries / retried
    assert "at most" in body or "twice" in body or "two" in body
    assert "report" in body
    # (3) Prefer non-interactive invocations to avoid pagers/prompts.
    assert "non-interactive" in body or "--no-pager" in body


# ─── Unknown-tool probing — the DEFAULT_DRIVER fallback (the "throw any CLI ──
# tool at clive" case, gh#41) must teach the agent to DISCOVER an unfamiliar
# tool before using it: probe with `--help`/`--version`/`man <tool> | cat`,
# infer flags from the help text, never blind-launch interactive TUIs/editors
# or credential-prompting commands, and pipe pager-y output through cat/head.
# This is the discovery-on-the-fly path — distinct from the bounded RECOVERY
# protocol (stuck/hung/retries/non-interactive) that the driver also carries.

def test_default_driver_constant_instructs_unknown_tool_probing():
    """The DEFAULT_DRIVER fallback constant must carry unknown-tool probing."""
    low = DEFAULT_DRIVER.lower()
    # (1) Probe an unfamiliar tool before using it.
    assert "probe" in low
    assert "--help" in low and "--version" in low
    assert "man <tool>" in low
    # (2) Infer flags from the help text.
    assert "flag" in low
    assert "help text" in low
    # (3) Never blind-launch interactive TUIs/editors or credential prompts.
    assert "interactive" in low
    assert "credential" in low
    # (4) Pipe pager-y output through cat/head.
    assert "| cat" in low or "| head" in low


def test_fallback_path_emits_probing_for_unknown_app_type(tmp_path):
    """load_driver for an app_type with NO driver file falls back to
    DEFAULT_DRIVER, which must carry the unknown-tool probing guidance."""
    import prompts as prompts_mod
    prompts_mod._driver_cache.clear()
    prompts_mod._driver_meta_cache.clear()
    body = load_driver("some_unknown_cli_tool", drivers_dir=str(tmp_path)).lower()
    assert "probe" in body
    assert "--help" in body
    assert "credential" in body


def test_default_md_driver_instructs_unknown_tool_probing():
    """load_driver('default') (drivers/default.md, the unknown-tool driver)
    must also carry the probing guidance."""
    import prompts as prompts_mod
    prompts_mod._driver_cache.clear()
    prompts_mod._driver_meta_cache.clear()
    body = load_driver("default").lower()
    assert "probe" in body
    assert "--help" in body and "--version" in body
    assert "credential" in body


def test_known_driver_unaffected_by_probing_block(tmp_path):
    """A known driver (its own .md file) is returned verbatim — the
    unknown-tool probing block is NOT grafted onto a real driver."""
    import prompts as prompts_mod
    prompts_mod._driver_cache.clear()
    prompts_mod._driver_meta_cache.clear()
    driver_file = tmp_path / "mytool.md"
    driver_file.write_text("# My tool driver\nDo the specific thing.")
    result = load_driver("mytool", drivers_dir=str(tmp_path))
    assert "My tool driver" in result
    # The fallback probing block must not leak into a real driver.
    assert "probe" not in result.lower()
    assert "--help" not in result


def test_load_existing_driver(tmp_path):
    driver_file = tmp_path / "shell.md"
    driver_file.write_text("# Shell driver\nKEYS: ctrl-c=interrupt")
    result = load_driver("shell", drivers_dir=str(tmp_path))
    assert "Shell driver" in result
    assert "ctrl-c=interrupt" in result


def test_load_missing_driver_returns_default(tmp_path):
    result = load_driver("nonexistent_tool", drivers_dir=str(tmp_path))
    assert result  # should return the default driver, not empty
    assert "autonomous agent" in result.lower() or "worker" in result.lower() or "control" in result.lower()


def test_load_driver_from_real_drivers_dir():
    """Once we create drivers/default.md, this should find it via fallback."""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    drivers_dir = os.path.join(project_dir, "src", "clive", "drivers")
    # Test fallback — no driver for 'nonexistent' should return default
    result = load_driver("nonexistent", drivers_dir=drivers_dir)
    assert len(result) > 10


# ─── Driver quarantine — load_driver behavior (gh#41 scenario #50) ──────────
# Auto-gen drivers land in drivers/.unreviewed/ and must NOT be loaded by
# default — load_driver only looks at drivers/<app_type>.md. The escape
# hatch CLIVE_TRUST_UNREVIEWED=1 lets evals and CI opt into loading
# unreviewed drivers without manually promoting them.

def test_load_driver_ignores_unreviewed_by_default(tmp_path, monkeypatch):
    """An unreviewed driver in drivers/.unreviewed/foo.md does NOT load —
    load_driver returns the DEFAULT_DRIVER fallback instead."""
    import prompts as prompts_mod
    # Bust the cache that previous tests may have populated for "foo".
    prompts_mod._driver_cache.clear()
    prompts_mod._driver_meta_cache.clear()

    unreviewed = tmp_path / ".unreviewed"
    unreviewed.mkdir()
    (unreviewed / "foo.md").write_text(
        "---\npreferred_mode: script\n---\n# foo driver (unreviewed)"
    )
    # No drivers/foo.md exists.
    result = load_driver("foo", drivers_dir=str(tmp_path))
    # Falls back to DEFAULT_DRIVER, NOT the .unreviewed copy.
    assert "foo driver (unreviewed)" not in result


def test_load_driver_unreviewed_loaded_when_env_var_set(tmp_path, monkeypatch):
    """CLIVE_TRUST_UNREVIEWED=1 opens the escape hatch — useful for evals."""
    import prompts as prompts_mod
    prompts_mod._driver_cache.clear()
    prompts_mod._driver_meta_cache.clear()

    unreviewed = tmp_path / ".unreviewed"
    unreviewed.mkdir()
    (unreviewed / "bar.md").write_text(
        "---\npreferred_mode: script\n---\n# bar driver (unreviewed body)"
    )
    monkeypatch.setenv("CLIVE_TRUST_UNREVIEWED", "1")
    result = load_driver("bar", drivers_dir=str(tmp_path))
    assert "bar driver (unreviewed body)" in result


def test_load_driver_reviewed_takes_precedence_over_unreviewed(tmp_path, monkeypatch):
    """If both drivers/<n>.md and drivers/.unreviewed/<n>.md exist,
    the reviewed copy wins — even with CLIVE_TRUST_UNREVIEWED=1."""
    import prompts as prompts_mod
    prompts_mod._driver_cache.clear()
    prompts_mod._driver_meta_cache.clear()

    (tmp_path / "baz.md").write_text("---\nx\n---\n# baz REVIEWED")
    unreviewed = tmp_path / ".unreviewed"
    unreviewed.mkdir()
    (unreviewed / "baz.md").write_text("---\nx\n---\n# baz UNREVIEWED")
    monkeypatch.setenv("CLIVE_TRUST_UNREVIEWED", "1")
    result = load_driver("baz", drivers_dir=str(tmp_path))
    assert "REVIEWED" in result
    assert "UNREVIEWED" not in result
