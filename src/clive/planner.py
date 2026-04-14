"""Compatibility shim — aliases planning.planner."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("planning.planner")
_sys.modules[__name__] = _real
