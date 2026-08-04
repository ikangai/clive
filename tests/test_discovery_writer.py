"""Tests for discovery.generator.write_generated_driver — pure file IO."""
import pytest

from discovery.generator import write_generated_driver


def test_writes_driver_to_drivers_dir(tmp_path):
    path = write_generated_driver(
        "rg", "---\npreferred_mode: script\n---\n# rg\n",
        drivers_dir=str(tmp_path),
    )
    assert path == str(tmp_path / "rg.md")
    assert (tmp_path / "rg.md").read_text().startswith("---")


def test_refuses_to_overwrite_existing_driver(tmp_path):
    (tmp_path / "rg.md").write_text("# hand-written")
    with pytest.raises(FileExistsError):
        write_generated_driver("rg", "new", drivers_dir=str(tmp_path))
    # Original untouched — handcrafted drivers must not be clobbered.
    assert (tmp_path / "rg.md").read_text() == "# hand-written"


def test_overwrite_flag_replaces(tmp_path):
    (tmp_path / "rg.md").write_text("# hand-written")
    written = "---\nx\n---\n"
    write_generated_driver("rg", written, drivers_dir=str(tmp_path), overwrite=True)
    assert (tmp_path / "rg.md").read_text() == written


@pytest.mark.parametrize("bad_name", [
    "../etc/passwd",
    "/etc/passwd",
    "rg/../etc/passwd",
    "..",
    ".",
    "",
    "rg name with space",
    "-leading-dash",       # leading non-alnum
    "RG",                  # uppercase — case-insensitive FS overwrite (Bug 6)
    "Foo",                 # mixed case
    "foo.md",              # confusing filename (Bug 46/scenario)
    "example.com",         # looks like a hostname
    "tool+plus",           # `+` not in tightened set
    "a..b",                # double-dot
])
def test_refuses_unsafe_tool_name(tmp_path, bad_name):
    with pytest.raises(ValueError, match="unsafe tool name|reserved"):
        write_generated_driver(bad_name, "---\nx\n---\n",
                               drivers_dir=str(tmp_path))


# ─── Reserved driver-name guard (gh#41 debug Bug 5) ─────────────────────────
# Discovery must not overwrite the meta-driver `explore.md` (its own driver)
# or any of the core hand-written drivers shipped in src/clive/drivers/.

@pytest.mark.parametrize("reserved", [
    "explore", "shell", "browser", "data", "docs",
    "default", "email", "email_cli", "agent", "media", "room",
])
def test_refuses_reserved_driver_names(tmp_path, reserved):
    with pytest.raises(ValueError, match="reserved"):
        write_generated_driver(reserved, "---\nx\n---\n",
                               drivers_dir=str(tmp_path),
                               overwrite=True)  # even with overwrite=True


def test_uses_default_unreviewed_dir_when_none(tmp_path, monkeypatch):
    # gh#41 quarantine (scenario #50): the default destination is the
    # quarantined ``drivers/.unreviewed/`` subdir, NOT the canonical
    # ``drivers/``. Auto-gen drivers don't land in the active driver set
    # until promoted via clive --promote-driver.
    # The destination now derives from $CLIVE_HOME rather than the install
    # dir, so re-home the process instead of patching module constants.
    monkeypatch.setenv("CLIVE_HOME", str(tmp_path))
    drivers = tmp_path / "drivers"
    path = write_generated_driver("rg2", "---\nx\n---\n")
    assert path == str(drivers / ".unreviewed" / "rg2.md")
    assert (drivers / ".unreviewed" / "rg2.md").exists()
    # And critically NOT in the reviewed location:
    assert not (drivers / "rg2.md").exists()


# ─── Driver quarantine (gh#41 scenario #50) ─────────────────────────────────
# Auto-gen drivers must land in drivers/.unreviewed/ by default so that
# load_driver (which only checks drivers/<app_type>.md) cannot pick them up
# until a human reviews + promotes them. This blunts every remaining
# prompt-injection vector that survives the regex-level content filters.

def test_promote_driver_moves_from_unreviewed_to_reviewed(tmp_path):
    from discovery.generator import promote_driver

    unreviewed = tmp_path / ".unreviewed"
    unreviewed.mkdir()
    driver_text = (
        "---\npreferred_mode: script\n---\n"
        "# rg Driver\n\nENVIRONMENT: rg\nPRIMARY TOOLS:\n- rg\n"
        "PATTERNS:\n- x\nPITFALLS:\n- y\nRESPONSE FORMAT:\n- bash\n"
        "COMPLETION: DONE: x\n"
    )
    (unreviewed / "rg.md").write_text(driver_text)

    new_path = promote_driver(
        "rg",
        drivers_dir=str(tmp_path),
        unreviewed_dir=str(unreviewed),
    )

    assert new_path == str(tmp_path / "rg.md")
    assert (tmp_path / "rg.md").read_text() == driver_text
    # Source no longer exists — atomic move
    assert not (unreviewed / "rg.md").exists()


def test_promote_driver_refuses_when_reviewed_already_exists(tmp_path):
    from discovery.generator import promote_driver

    (tmp_path / "rg.md").write_text("# hand-written\n")
    unreviewed = tmp_path / ".unreviewed"
    unreviewed.mkdir()
    (unreviewed / "rg.md").write_text("---\nfresh\n---\n")

    with pytest.raises(FileExistsError):
        promote_driver(
            "rg",
            drivers_dir=str(tmp_path),
            unreviewed_dir=str(unreviewed),
        )
    # Original untouched
    assert (tmp_path / "rg.md").read_text() == "# hand-written\n"
    # Unreviewed copy preserved
    assert (unreviewed / "rg.md").exists()


def test_promote_driver_force_replaces_reviewed(tmp_path):
    from discovery.generator import promote_driver

    (tmp_path / "rg.md").write_text("# hand-written\n")
    unreviewed = tmp_path / ".unreviewed"
    unreviewed.mkdir()
    valid = (
        "---\npreferred_mode: script\n---\n"
        "# rg Driver\n\nENVIRONMENT: rg\nPRIMARY TOOLS:\n- rg\n"
        "PATTERNS:\n- x\nPITFALLS:\n- y\nRESPONSE FORMAT:\n- bash\n"
        "COMPLETION: DONE: x\n"
    )
    (unreviewed / "rg.md").write_text(valid)

    promote_driver(
        "rg",
        drivers_dir=str(tmp_path),
        unreviewed_dir=str(unreviewed),
        force=True,
    )
    assert (tmp_path / "rg.md").read_text() == valid


def test_promote_driver_refuses_missing_unreviewed_source(tmp_path):
    from discovery.generator import promote_driver

    unreviewed = tmp_path / ".unreviewed"
    unreviewed.mkdir()
    with pytest.raises(FileNotFoundError):
        promote_driver(
            "nonexistent",
            drivers_dir=str(tmp_path),
            unreviewed_dir=str(unreviewed),
        )


def test_promote_driver_validates_unsafe_name(tmp_path):
    from discovery.generator import promote_driver

    with pytest.raises(ValueError, match="unsafe tool name|reserved"):
        promote_driver(
            "../../etc/passwd",
            drivers_dir=str(tmp_path),
            unreviewed_dir=str(tmp_path),
        )


def test_promote_driver_refuses_reserved_name(tmp_path):
    from discovery.generator import promote_driver

    with pytest.raises(ValueError, match="reserved"):
        promote_driver(
            "explore",
            drivers_dir=str(tmp_path),
            unreviewed_dir=str(tmp_path),
        )


def test_promote_driver_validates_content_before_moving(tmp_path):
    """If the unreviewed driver is structurally broken, promote refuses
    so a corrupt driver can't slip into the canonical location."""
    from discovery.generator import promote_driver

    unreviewed = tmp_path / ".unreviewed"
    unreviewed.mkdir()
    # Missing PITFALLS, missing frontmatter — broken.
    (unreviewed / "rg.md").write_text("# rg\nno sections here\n")

    with pytest.raises(ValueError, match="missing|frontmatter"):
        promote_driver(
            "rg",
            drivers_dir=str(tmp_path),
            unreviewed_dir=str(unreviewed),
        )
    # Source preserved; nothing landed in reviewed
    assert (unreviewed / "rg.md").exists()
    assert not (tmp_path / "rg.md").exists()


# ─── TOCTOU race regression (gh#41 debug Bug 3) ─────────────────────────────
# The previous implementation used ``os.path.exists(path) and open(path, 'w')``
# in two separate steps. Across forked processes, half the racers slipped past
# the exists() check and silently truncated each other's writes via O_TRUNC.
# Fix: open with O_EXCL ("x" mode) for non-overwrite; tmp-then-rename for
# overwrite. ``fork`` is used (not multiprocessing) so the test stays POSIX.

def _fork_race_workers(tmpdir, overwrite, n=30):
    import os, time
    barrier = os.path.join(tmpdir, ".go")
    children = []
    for i in range(n):
        pid = os.fork()
        if pid == 0:
            while not os.path.exists(barrier):
                pass
            try:
                write_generated_driver(
                    "rg",
                    f"---\npreferred_mode: script\n---\n# proc{i}\n",
                    drivers_dir=tmpdir, overwrite=overwrite,
                )
                os._exit(0)   # wrote
            except FileExistsError:
                os._exit(1)
            except Exception:
                os._exit(2)
        children.append(pid)
    time.sleep(0.3)
    open(barrier, "w").close()
    oks = ex = other = 0
    for pid in children:
        _, st = os.waitpid(pid, 0)
        code = os.WEXITSTATUS(st)
        oks += (code == 0); ex += (code == 1); other += (code != 0 and code != 1)
    os.unlink(barrier)
    return oks, ex, other


def test_concurrent_non_overwrite_only_one_writer_wins(tmp_path):
    oks, exists_err, other = _fork_race_workers(str(tmp_path), overwrite=False, n=30)
    # Exactly one process wrote; the rest must have seen FileExistsError.
    assert oks == 1, f"expected 1 writer to win, got {oks} (TOCTOU race)"
    assert exists_err == 29
    assert other == 0


def test_concurrent_overwrite_is_atomic(tmp_path):
    # With overwrite=True, all processes "succeed", but the final file must
    # be one of the racer's contents in full — never a partial mix.
    oks, exists_err, other = _fork_race_workers(str(tmp_path), overwrite=True, n=30)
    assert oks == 30
    body = (tmp_path / "rg.md").read_text()
    # Must start with frontmatter delimiter and contain exactly one #procN line.
    assert body.startswith("---\npreferred_mode: script\n---\n")
    assert body.count("# proc") == 1


def test_overwrite_uses_atomic_rename(tmp_path, monkeypatch):
    # If overwrite=True uses an atomic rename, os.replace is called exactly
    # once. If it uses a non-atomic open(path, "w"), os.replace isn't called.
    import os as _os
    (tmp_path / "rg.md").write_text("# original")
    call_count = {"n": 0}
    orig_replace = _os.replace
    def counting_replace(src, dst):
        call_count["n"] += 1
        return orig_replace(src, dst)
    monkeypatch.setattr("discovery.generator.os.replace", counting_replace)
    write_generated_driver("rg", "---\nnew\n---\n",
                           drivers_dir=str(tmp_path), overwrite=True)
    assert call_count["n"] == 1, (
        "write_generated_driver(overwrite=True) must use os.replace for "
        "an atomic swap; got %d calls (the non-atomic open(path, 'w') path "
        "would leave a truncate window visible to concurrent readers)"
        % call_count["n"]
    )
    assert (tmp_path / "rg.md").read_text() == "---\nnew\n---\n"


# ─── Review follow-ups: cache + migration ─────────────────────────────

def test_promote_invalidates_the_driver_cache(tmp_path, monkeypatch):
    """Learn-then-use in ONE session is the point of home-local learning.

    A lookup before promotion caches DEFAULT_DRIVER for that app_type;
    without invalidation the promoted driver stays invisible until the
    process restarts.
    """
    import prompts
    from discovery.generator import promote_driver, write_generated_driver

    monkeypatch.setenv("CLIVE_HOME", str(tmp_path))
    prompts._driver_cache.clear()
    prompts._driver_meta_cache.clear()

    body = "---\n---\nENVIRONMENT\nPRIMARY TOOLS\nPATTERNS\nPITFALLS\nRESPONSE FORMAT\nCOMPLETION\n"
    write_generated_driver("mytool", body)

    # Miss first — quarantined drivers are not loadable.
    assert prompts.load_driver("mytool") == prompts.DEFAULT_DRIVER
    promote_driver("mytool")
    # Must now be visible without a restart.
    assert prompts.load_driver("mytool") != prompts.DEFAULT_DRIVER

    prompts._driver_cache.clear()
    prompts._driver_meta_cache.clear()


def test_cache_key_is_scoped_to_the_agent_home(tmp_path):
    """Resolution depends on $CLIVE_HOME, so the key must too."""
    import os
    import prompts
    a = prompts.driver_cache_key("x")
    old = os.environ.get("CLIVE_HOME")
    os.environ["CLIVE_HOME"] = str(tmp_path / "other")
    try:
        assert prompts.driver_cache_key("x") != a
    finally:
        if old is None:
            os.environ.pop("CLIVE_HOME", None)
        else:
            os.environ["CLIVE_HOME"] = old


def test_legacy_install_quarantine_is_still_promotable(tmp_path, monkeypatch):
    """Drivers quarantined before learning moved to $CLIVE_HOME must not
    become permanently un-promotable."""
    import prompts
    from discovery.generator import promote_driver

    legacy_install = tmp_path / "install_drivers"
    (legacy_install / ".unreviewed").mkdir(parents=True)
    body = "---\n---\nENVIRONMENT\nPRIMARY TOOLS\nPATTERNS\nPITFALLS\nRESPONSE FORMAT\nCOMPLETION\n"
    (legacy_install / ".unreviewed" / "oldtool.md").write_text(body)

    monkeypatch.setenv("CLIVE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("discovery.generator._DRIVERS_DIR", str(legacy_install))

    dst = promote_driver("oldtool")
    # Promoted INTO the home, i.e. migrated to the new layout.
    assert dst == str(tmp_path / "home" / "drivers" / "oldtool.md")
    assert not (legacy_install / ".unreviewed" / "oldtool.md").exists()
    prompts._driver_cache.clear()
