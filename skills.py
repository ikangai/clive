"""Skills system — procedural recipes for common tasks.

Skills are markdown files in the skills/ directory. Unlike drivers (static
reference cards), skills are step-by-step procedures that guide the agent
through multi-step workflows.

Features:
- Discovery: skills/{name}.md or skills/{category}/{name}.md
- Parameters: [skill:api-test url=https://api.example.com]
- Composition: skills can reference other skills via [use:other-skill]
- Metadata: YAML frontmatter for tags, params, required tools
- Generation: agents can save new skills during execution
"""
import os
import re

_SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
_skill_cache: dict[str, str] = {}


def load_skill(name: str, skills_dir: str | None = None) -> str | None:
    """Load a skill by name. Returns None if not found.

    Searches:
    1. skills/{name}.md (flat)
    2. skills/{category}/{name}.md (categorized)
    """
    cache_key = f"{name}:{skills_dir or 'default'}"
    if cache_key in _skill_cache:
        return _skill_cache[cache_key]

    base = skills_dir or _SKILLS_DIR

    # Direct match
    path = os.path.join(base, f"{name}.md")
    if os.path.exists(path):
        content = _read_skill(path)
        _skill_cache[cache_key] = content
        return content

    # Search in subdirectories (categories)
    if os.path.isdir(base):
        for subdir in os.listdir(base):
            subpath = os.path.join(base, subdir, f"{name}.md")
            if os.path.isfile(subpath):
                content = _read_skill(subpath)
                _skill_cache[cache_key] = content
                return content

    return None


def _read_skill(path: str) -> str:
    """Read a skill file, stripping frontmatter if present."""
    with open(path, "r") as f:
        content = f.read().strip()
    # Strip YAML frontmatter (---...---)
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    return content


def resolve_skill_with_params(skill_ref: str) -> tuple[str | None, dict]:
    """Parse a skill reference like 'api-test url=https://example.com'.

    Returns (skill_name, params_dict).
    """
    parts = skill_ref.strip().split(None, 1)
    name = parts[0] if parts else ""
    params = {}

    if len(parts) > 1:
        # Parse key=value pairs
        for m in re.finditer(r'(\w+)=(\S+)', parts[1]):
            params[m.group(1)] = m.group(2)

    return name, params


def inject_params(skill_content: str, params: dict) -> str:
    """Replace {PARAM} placeholders in skill content with actual values."""
    result = skill_content
    for key, value in params.items():
        result = result.replace(f"{{{key.upper()}}}", value)
        result = result.replace(f"{{{key}}}", value)
    return result


def resolve_composition(skill_content: str, skills_dir: str | None = None) -> str:
    """Resolve [use:other-skill] references in a skill, inlining them."""
    def _replace(match):
        ref_name = match.group(1)
        ref_skill = load_skill(ref_name, skills_dir)
        if ref_skill:
            return f"\n--- Included skill: {ref_name} ---\n{ref_skill}\n--- End {ref_name} ---\n"
        return f"[skill {ref_name} not found]"

    return re.sub(r'\[use:(\w[\w-]*)\]', _replace, skill_content)


def list_skills(skills_dir: str | None = None) -> list[dict]:
    """List all available skills with name, description, and category."""
    base = skills_dir or _SKILLS_DIR
    skills = []
    if not os.path.isdir(base):
        return skills

    # Flat skills
    for fname in sorted(os.listdir(base)):
        if fname.endswith(".md"):
            path = os.path.join(base, fname)
            skills.append(_skill_info(fname[:-3], path, category=""))

    # Categorized skills (subdirectories)
    for subdir in sorted(os.listdir(base)):
        subpath = os.path.join(base, subdir)
        if os.path.isdir(subpath) and not subdir.startswith("."):
            for fname in sorted(os.listdir(subpath)):
                if fname.endswith(".md"):
                    path = os.path.join(subpath, fname)
                    skills.append(_skill_info(fname[:-3], path, category=subdir))

    return skills


def _skill_info(name: str, path: str, category: str) -> dict:
    """Extract skill metadata from file."""
    with open(path, "r") as f:
        first_line = f.readline().strip().lstrip("# ")
    # Check for frontmatter tags
    tags = []
    params = []
    with open(path, "r") as f:
        content = f.read()
    if content.startswith("---"):
        fm_end = content.find("---", 3)
        if fm_end != -1:
            fm = content[3:fm_end]
            # Extract tags: line
            tag_match = re.search(r'tags:\s*(.+)', fm)
            if tag_match:
                tags = [t.strip() for t in tag_match.group(1).split(",")]
            # Extract params: line
            param_match = re.search(r'params:\s*(.+)', fm)
            if param_match:
                params = [p.strip() for p in param_match.group(1).split(",")]

    return {
        "name": name,
        "description": first_line,
        "category": category,
        "tags": tags,
        "params": params,
    }


def get_skill_names(skills_dir: str | None = None) -> list[str]:
    """Get just the names of available skills."""
    return [s["name"] for s in list_skills(skills_dir)]


def save_skill(name: str, content: str, category: str = "", skills_dir: str | None = None) -> str:
    """Save a new skill to the skills directory. Returns the file path."""
    base = skills_dir or _SKILLS_DIR
    if category:
        target_dir = os.path.join(base, category)
        os.makedirs(target_dir, exist_ok=True)
    else:
        target_dir = base
        os.makedirs(target_dir, exist_ok=True)

    path = os.path.join(target_dir, f"{name}.md")
    with open(path, "w") as f:
        f.write(content)

    # Invalidate cache
    cache_key = f"{name}:{skills_dir or 'default'}"
    _skill_cache.pop(cache_key, None)

    return path


def skills_summary_for_planner(skills_dir: str | None = None) -> str:
    """Build a compact skills summary for the planner prompt."""
    skills = list_skills(skills_dir)
    if not skills:
        return ""

    lines = ["\nAvailable skills (assign to subtasks via \"skill\" field):"]
    for s in skills:
        cat = f"[{s['category']}] " if s.get("category") else ""
        params = f" (params: {', '.join(s['params'])})" if s.get("params") else ""
        lines.append(f"  - {cat}{s['name']}: {s['description']}{params}")

    return "\n".join(lines)
