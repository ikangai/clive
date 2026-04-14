"""Compatibility shim — aliases execution.runtime."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("execution.runtime")
_sys.modules[__name__] = _real
