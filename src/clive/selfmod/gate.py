"""Deterministic gate — the immutable safety anchor.

This file is IMMUTABLE. No LLM role can modify it. It performs deterministic
scanning on proposed changes and has unconditional veto power.

For .py files the gate parses the source with ``ast`` and walks the syntax
tree (catches the regex bypasses documented in Bug H6: arbitrary-arg
``subprocess.run(fn(), shell=True)``, ``from ctypes import X``, ``urllib3``,
``websockets``, ...). For non-Python content the regex BANNED_PATTERNS set
still applies.

The gate cannot be "talked past" — it runs deterministic pattern matching
and tree walking, not LLM inference.
"""

import ast
import re
from pathlib import Path

from selfmod.constitution import get_tier, highest_tier, required_approvals

# Regex patterns for non-Python content (markdown, shell, config). For .py
# files the AST scanner below is the primary defense; the obfuscated-base64
# regex is also applied to .py since it's a content-shape pattern AST can't
# see as conveniently.
BANNED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"os\.system\s*\("), "os.system() call"),
    (re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"), "subprocess with shell=True"),
    (re.compile(r"(?<!\w)eval\s*\("), "eval() call"),
    (re.compile(r"(?<!\w)exec\s*\("), "exec() call"),
    (re.compile(r"import\s+ctypes"), "ctypes import"),
    (re.compile(r"importlib\.reload\s*\("), "importlib.reload()"),
    (re.compile(r"['\"][A-Za-z0-9+/=]{100,}['\"]"), "obfuscated base64 string"),
    (re.compile(r"(?:urllib|requests|httpx|socket)\b"), "network library in selfmod"),
    (re.compile(r"__import__\s*\("), "dynamic __import__()"),
]


# AST scanner taxonomy (Bug H6). Banned at the import boundary regardless
# of file location — ctypes opens FFI which is unrestricted execution.
_BANNED_ALWAYS_MODULES = frozenset({"ctypes"})

# Network libraries are banned only inside selfmod/ (exfiltration vector).
_BANNED_SELFMOD_MODULES = frozenset({
    "urllib", "urllib2", "urllib3",
    "requests", "httpx", "socket", "websockets",
})

# Bare-name builtins that execute arbitrary code.
_BANNED_BUILTINS = frozenset({"eval", "exec", "__import__"})

# Attribute calls on module-name receivers — (receiver, attr) → description.
# These match `os.system(...)`, `os.popen(...)`, `importlib.reload(...)`.
_BANNED_ATTR_CALLS: dict[tuple[str, str], str] = {
    ("os", "system"): "os.system() call",
    ("os", "popen"): "os.popen() call",
    ("importlib", "reload"): "importlib.reload() call",
}


def _module_top_level(name: str) -> str:
    """Top-level package from a dotted module path (`ctypes.util` → `ctypes`)."""
    return name.split(".", 1)[0]


def _scan_python_ast(filepath: str, content: str, is_selfmod: bool) -> list[str]:
    """Walk the AST of a Python source file and return banned-pattern violations.

    Catches the structural patterns the regex set could be rewritten around
    (Bug H6): arbitrary-arg ``shell=True``, ``from ctypes import X``,
    ``import urllib3`` / ``websockets``, ``getattr``-style dynamic dispatch
    is not handled here (deliberately scope-limited to the four bypasses
    the audit identified plus os.popen for parity with os.system).
    """
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        # Parse failure is rejected outright — silently allowing unparseable
        # Python would itself be a bypass (an attacker emits malformed Python
        # to skip scanning, then the runtime fails to import it harmlessly,
        # but the FILE landed on disk and another agent's parser might tolerate).
        return [f"{filepath}: cannot parse as Python ({e.msg})"]

    violations: list[str] = []
    for node in ast.walk(tree):
        # Import sinks.
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _module_top_level(alias.name)
                if top in _BANNED_ALWAYS_MODULES:
                    violations.append(f"{filepath}: banned import: {alias.name}")
                elif is_selfmod and top in _BANNED_SELFMOD_MODULES:
                    violations.append(
                        f"{filepath}: banned network library in selfmod: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = _module_top_level(node.module)
                if top in _BANNED_ALWAYS_MODULES:
                    violations.append(f"{filepath}: banned import: from {node.module}")
                elif is_selfmod and top in _BANNED_SELFMOD_MODULES:
                    violations.append(
                        f"{filepath}: banned network library in selfmod: from {node.module}"
                    )

        # Call sinks.
        if isinstance(node, ast.Call):
            func = node.func
            # Bare-name builtins: eval, exec, __import__.
            if isinstance(func, ast.Name) and func.id in _BANNED_BUILTINS:
                violations.append(f"{filepath}: banned call: {func.id}()")
            # Attribute calls: os.system, os.popen, importlib.reload.
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                key = (func.value.id, func.attr)
                if key in _BANNED_ATTR_CALLS:
                    violations.append(f"{filepath}: {_BANNED_ATTR_CALLS[key]}")
                # subprocess.<anything>(..., shell=True) — argument complexity
                # is irrelevant; the keyword check is what matters.
                if func.value.id == "subprocess":
                    for kw in node.keywords:
                        if (
                            kw.arg == "shell"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value is True
                        ):
                            violations.append(
                                f"{filepath}: subprocess.{func.attr}() with shell=True"
                            )
                            break

    return violations

# Paths that can never be touched. Keys are package-relative (under
# src/clive/) so workspace.apply_changes (PROJECT_ROOT=src/clive/) writes
# to the same place this gate inspects. The src/clive/ rooting is also
# what constitution.get_tier uses, so all three modules agree on the path
# topology. Audit C2 (2026-05-27).
ABSOLUTE_IMMUTABLE = {
    "selfmod/gate.py",
    ".clive/constitution.md",
}

# Unified with constitution.PROJECT_ROOT and workspace.PROJECT_ROOT — all
# three now resolve to src/clive/. The historical 4-parent value (repo
# root) meant absolute-path proposals normalized to "src/clive/selfmod/
# gate.py" which wasn't a FILE_TIERS key, so audit C2 reported they could
# default to OPEN tier. In practice constitution.get_tier's .relative_to
# (src/clive/) coincidentally reduced the path and caught the case, but
# the divergence was a topology smell and broke for any path shape whose
# .resolve() landed outside src/clive/ (e.g. `../selfmod/gate.py`).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def check_proposal(
    files: dict[str, str],
    approvals: dict[str, str],
) -> dict:
    """Check a modification proposal against deterministic rules.

    Args:
        files: mapping of relative filepath → proposed new content
        approvals: mapping of role name → approval status ("approved"/"rejected"/"advisory")

    Returns:
        dict with keys:
            allowed: bool
            reason: str (human-readable)
            violations: list of specific violations found
    """
    violations = []

    # 0. Path-shape gate (Audit C2). Proposals must use simple relative
    # paths under the workspace. Absolute paths and `..` segments are
    # rejected up front so the downstream tier lookup operates on a
    # canonical form — never on a shape whose `.resolve()` could land
    # outside the workspace and silently degrade to OPEN tier.
    for filepath in files:
        if not isinstance(filepath, str) or not filepath:
            violations.append(f"INVALID-PATH: empty or non-string filepath {filepath!r}")
            continue
        if Path(filepath).is_absolute():
            violations.append(f"INVALID-PATH: absolute paths not permitted in proposals ({filepath})")
            continue
        if ".." in Path(filepath).parts:
            violations.append(f"INVALID-PATH: `..` traversal not permitted ({filepath})")

    if violations:
        return {"allowed": False, "reason": "Invalid path shape", "violations": violations}

    # 1. Check immutable files
    for filepath in files:
        rel = _normalize_path(filepath)
        if rel in ABSOLUTE_IMMUTABLE:
            violations.append(f"IMMUTABLE: cannot modify {rel}")
        if rel.startswith(".clive/audit/"):
            violations.append(f"IMMUTABLE: cannot modify audit trail ({rel})")

    if violations:
        return {"allowed": False, "reason": "Immutable files targeted", "violations": violations}

    # 2. Check tier-based approval requirements
    paths = list(files.keys())
    tier = highest_tier(paths)
    required = required_approvals(tier)

    if required is None:
        violations.append(f"Tier {tier}: modifications not allowed")
        return {"allowed": False, "reason": f"Tier {tier} is immutable", "violations": violations}

    missing = []
    for role in required:
        status = approvals.get(role, "missing")
        if status != "approved":
            missing.append(f"{role}: {status}")

    if missing:
        violations.append(f"Tier {tier} requires: {', '.join(required)}. Missing/rejected: {', '.join(missing)}")

    # 3. Scan content for banned patterns.
    #
    # For .py files the AST walker is the primary defense (catches the
    # H6 regex bypasses — arbitrary-arg shell=True, from-form ctypes,
    # urllib3, websockets). The obfuscated-base64 regex is content-shape
    # and still applies to .py files. For non-Python content the regex
    # set runs in full as before.
    for filepath, content in files.items():
        rel = _normalize_path(filepath)
        is_selfmod = rel.startswith("selfmod/")

        if filepath.endswith(".py"):
            violations.extend(_scan_python_ast(rel, content, is_selfmod))
            # Content-shape regex still applies to .py (obfuscated base64).
            for pattern, description in BANNED_PATTERNS:
                if "base64" in description and pattern.search(content):
                    violations.append(f"{rel}: banned pattern: {description}")
        else:
            for pattern, description in BANNED_PATTERNS:
                # Network library check only applies to selfmod modules.
                if "network library" in description and not is_selfmod:
                    continue
                matches = pattern.findall(content)
                if matches:
                    violations.append(f"{rel}: banned pattern: {description}")

    # 4. Check for file tier escalation attempts
    for filepath, content in files.items():
        rel = _normalize_path(filepath)
        # If the file modifies constitution.py's FILE_TIERS, reject
        if rel == "selfmod/constitution.py" and "FILE_TIERS" in content:
            # Check if any tier was downgraded
            violations.append(f"{rel}: potential tier escalation (modifies FILE_TIERS)")

    if violations:
        return {
            "allowed": False,
            "reason": f"Gate rejected: {len(violations)} violation(s)",
            "violations": violations,
        }

    return {"allowed": True, "reason": "All checks passed", "violations": []}


def _normalize_path(filepath: str) -> str:
    """Normalize a file path to be relative to project root."""
    try:
        return str(Path(filepath).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return filepath
