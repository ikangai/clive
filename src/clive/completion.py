"""Compatibility shim — aliases observation.completion."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("observation.completion")
_sys.modules[__name__] = _real
