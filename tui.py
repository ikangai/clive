#!/usr/bin/env python3
"""Entry-point wrapper — forwards to src/clive/tui/tui.py."""
import os
import sys

_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "clive")
if _src not in sys.path:
    sys.path.insert(0, _src)

if __name__ == "__main__":
    import runpy
    runpy.run_path(os.path.join(_src, "tui", "tui.py"), run_name="__main__")
