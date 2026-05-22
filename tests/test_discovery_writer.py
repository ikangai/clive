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
    "-leading-dash",  # leading non-alnum
])
def test_refuses_path_traversal_in_tool_name(tmp_path, bad_name):
    with pytest.raises(ValueError, match="unsafe tool name"):
        write_generated_driver(bad_name, "x", drivers_dir=str(tmp_path))


def test_uses_default_drivers_dir_when_none(tmp_path, monkeypatch):
    # Confirm write_generated_driver routes through the canonical
    # _DRIVERS_DIR (re-exported from prompts.py), not a re-derived path.
    monkeypatch.setattr("discovery.generator._DRIVERS_DIR", str(tmp_path))
    path = write_generated_driver("rg2", "---\nx\n---\n")
    assert path == str(tmp_path / "rg2.md")
