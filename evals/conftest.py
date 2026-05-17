"""Exclude eval fixture projects from parent-repo pytest collection.

Each fixture under evals/.../fixtures/.../project/ is a standalone mini-codebase
the agent operates on during evaluation. Their test files import from their own
local `src/` package and must be run with their fixture directory as the cwd —
running them as part of the parent repo's pytest sweeps blows up collection
because `from src.utils import ...` resolves against the repo-root `src/clive/`
package instead.
"""
collect_ignore_glob = ["**/fixtures/**"]
