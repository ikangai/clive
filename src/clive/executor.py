"""Compatibility shim — aliases execution.executor."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("execution.executor")
_sys.modules[__name__] = _real
