"""Compatibility shim — aliases networking.registry."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.registry")
_sys.modules[__name__] = _real
