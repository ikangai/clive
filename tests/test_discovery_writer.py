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


def test_uses_default_drivers_dir_when_none(tmp_path, monkeypatch):
    # Confirm write_generated_driver routes through the canonical
    # _DRIVERS_DIR (re-exported from prompts.py), not a re-derived path.
    monkeypatch.setattr("discovery.generator._DRIVERS_DIR", str(tmp_path))
    path = write_generated_driver("rg2", "---\nx\n---\n")
    assert path == str(tmp_path / "rg2.md")


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
