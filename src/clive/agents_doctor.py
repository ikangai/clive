"""Compatibility shim — aliases networking.agents_doctor."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.agents_doctor")
_sys.modules[__name__] = _real
