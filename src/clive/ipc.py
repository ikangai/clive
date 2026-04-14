"""Compatibility shim — aliases networking.ipc."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.ipc")
_sys.modules[__name__] = _real
