"""Tests for skills system."""
import os
from skills import load_skill, list_skills, get_skill_names


def test_load_existing_skill():
    skill = load_skill("analyze-logs")
    assert skill is not None
    assert "PROCEDURE" in skill
    assert "ERROR" in skill


def test_load_missing_skill():
    skill = load_skill("nonexistent-skill-xyz")
    assert skill is None


def test_list_skills():
    skills = list_skills()
    assert len(skills) >= 5
    names = [s["name"] for s in skills]
    assert "analyze-logs" in names
    assert "backup" in names


def test_get_skill_names():
    names = get_skill_names()
    assert "api-test" in names
    assert "git-summary" in names


def test_load_skill_from_custom_dir(tmp_path):
    (tmp_path / "custom.md").write_text("# Custom\nDo the thing")
    skill = load_skill("custom", skills_dir=str(tmp_path))
    assert "Do the thing" in skill
