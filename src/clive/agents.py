"""Compatibility shim — aliases networking.agents."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.agents")
_sys.modules[__name__] = _real
