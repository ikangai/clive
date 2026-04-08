"""Tests for skills system — loading, params, composition, generation."""
import os
from skills import (
    load_skill, list_skills, get_skill_names,
    resolve_skill_with_params, inject_params, resolve_composition,
    save_skill, skills_summary_for_planner,
)


# ─── Basic loading ────────────────────────────────────────────────────────────

def test_load_existing_skill():
    skill = load_skill("analyze-logs")
    assert skill is not None
    assert "PROCEDURE" in skill


def test_load_missing_skill():
    assert load_skill("nonexistent-skill-xyz") is None


def test_list_skills():
    skills = list_skills()
    assert len(skills) >= 5
    names = [s["name"] for s in skills]
    assert "analyze-logs" in names
    assert "backup" in names


def test_get_skill_names():
    names = get_skill_names()
    assert "api-test" in names


def test_load_from_custom_dir(tmp_path):
    (tmp_path / "custom.md").write_text("# Custom\nDo the thing")
    skill = load_skill("custom", skills_dir=str(tmp_path))
    assert "Do the thing" in skill


# ─── Parameters ───────────────────────────────────────────────────────────────

def test_resolve_params_simple():
    name, params = resolve_skill_with_params("api-test url=https://example.com")
    assert name == "api-test"
    assert params == {"url": "https://example.com"}


def test_resolve_params_multiple():
    name, params = resolve_skill_with_params("backup target=/data dest=s3://bucket")
    assert name == "backup"
    assert params["target"] == "/data"
    assert params["dest"] == "s3://bucket"


def test_resolve_params_none():
    name, params = resolve_skill_with_params("analyze-logs")
    assert name == "analyze-logs"
    assert params == {}


def test_inject_params():
    content = "Check {URL} and save to {OUTPUT}"
    result = inject_params(content, {"url": "https://api.example.com", "output": "/tmp/result.json"})
    assert "https://api.example.com" in result
    assert "/tmp/result.json" in result


# ─── Composition ──────────────────────────────────────────────────────────────

def test_resolve_composition(tmp_path):
    (tmp_path / "helper.md").write_text("# Helper\nDo helper thing")
    content = "Step 1: setup\n[use:helper]\nStep 2: done"
    result = resolve_composition(content, skills_dir=str(tmp_path))
    assert "Do helper thing" in result
    assert "Step 1" in result
    assert "Step 2" in result


def test_resolve_composition_missing():
    content = "Step 1: [use:nonexistent-xyz]"
    result = resolve_composition(content)
    assert "not found" in result


def test_deploy_check_has_composition():
    skill = load_skill("deploy-check")
    assert skill is not None
    assert "[use:git-summary]" in skill


# ─── Generation ───────────────────────────────────────────────────────────────

def test_save_skill(tmp_path):
    path = save_skill("my-skill", "# My Skill\nDo stuff", skills_dir=str(tmp_path))
    assert os.path.exists(path)
    with open(path) as f:
        assert "Do stuff" in f.read()


def test_save_skill_in_category(tmp_path):
    path = save_skill("deploy", "# Deploy\nSteps...", category="ops", skills_dir=str(tmp_path))
    assert "/ops/" in path
    assert os.path.exists(path)


# ─── Planner integration ─────────────────────────────────────────────────────

def test_skills_summary_for_planner():
    summary = skills_summary_for_planner()
    assert "Available skills" in summary
    assert "analyze-logs" in summary


def test_frontmatter_stripped():
    skill = load_skill("deploy-check")
    assert "---" not in skill
    assert "Deploy Check" in skill


def test_frontmatter_tags():
    skills = list_skills()
    deploy = next((s for s in skills if s["name"] == "deploy-check"), None)
    assert deploy is not None
    assert "ops" in deploy.get("tags", [])
    assert "SERVICE" in deploy.get("params", [])
