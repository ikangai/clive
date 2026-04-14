"""Compatibility shim — aliases networking.remote."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.remote")
_sys.modules[__name__] = _real
