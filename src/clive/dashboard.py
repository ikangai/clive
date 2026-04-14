"""Compatibility shim — aliases networking.dashboard."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.dashboard")
_sys.modules[__name__] = _real
