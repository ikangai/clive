"""Compatibility shim — aliases execution.planned_runner."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("execution.planned_runner")
_sys.modules[__name__] = _real
