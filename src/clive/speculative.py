"""Compatibility shim — aliases execution.speculative."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("execution.speculative")
_sys.modules[__name__] = _real
