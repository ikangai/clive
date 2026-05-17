#!/usr/bin/env python3
"""Entry-point wrapper — forwards to src/clive/clive.py."""
import os
import sys

# Ensure src/clive/ is on the import path so flat imports work.
_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "clive")
if _src not in sys.path:
    sys.path.insert(0, _src)

if __name__ == "__main__":
    import runpy
    runpy.run_path(os.path.join(_src, "clive.py"), run_name="__main__")
else:
    # When imported as a module (e.g., `from clive import _is_direct` in tests),
    # re-export the same symbols src/clive/clive.py exposes. Without this,
    # `import clive` finds this wrapper first whenever pytest's package walking
    # puts the repo root ahead of src/clive on sys.path.
    from clive_core import _is_direct, run  # noqa: F401
