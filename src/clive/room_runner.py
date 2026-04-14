"""Compatibility shim — aliases execution.room_runner."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("execution.room_runner")
_sys.modules[__name__] = _real
