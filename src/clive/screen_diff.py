"""Compatibility shim — aliases observation.screen_diff."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("observation.screen_diff")
_sys.modules[__name__] = _real
