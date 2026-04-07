"""Skills system — procedural recipes for common tasks.

Skills are markdown files in the skills/ directory. Unlike drivers (static
reference cards), skills are step-by-step procedures that guide the agent
through multi-step workflows.

Discovery: skills/{name}.md
Invocation: the planner can assign a skill to a subtask via the "skill" field.
The skill content is injected into the worker prompt alongside the driver.
"""
import os

_SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
_skill_cache: dict[str, str] = {}


def load_skill(name: str, skills_dir: str | None = None) -> str | None:
    """Load a skill by name. Returns None if not found."""
    cache_key = f"{name}:{skills_dir or 'default'}"
    if cache_key in _skill_cache:
        return _skill_cache[cache_key]

    base = skills_dir or _SKILLS_DIR
    path = os.path.join(base, f"{name}.md")
    if os.path.exists(path):
        with open(path, "r") as f:
            content = f.read().strip()
        _skill_cache[cache_key] = content
        return content
    return None


def list_skills(skills_dir: str | None = None) -> list[dict]:
    """List all available skills with name and first-line description."""
    base = skills_dir or _SKILLS_DIR
    skills = []
    if not os.path.isdir(base):
        return skills
    for fname in sorted(os.listdir(base)):
        if fname.endswith(".md"):
            name = fname[:-3]
            path = os.path.join(base, fname)
            with open(path, "r") as f:
                first_line = f.readline().strip().lstrip("# ")
            skills.append({"name": name, "description": first_line})
    return skills


def get_skill_names(skills_dir: str | None = None) -> list[str]:
    """Get just the names of available skills."""
    return [s["name"] for s in list_skills(skills_dir)]
