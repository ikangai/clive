"""Compatibility shim — aliases execution.pane_loop."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("execution.pane_loop")
_sys.modules[__name__] = _real
