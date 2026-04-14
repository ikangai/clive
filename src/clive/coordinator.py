"""Compatibility shim — aliases networking.coordinator."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.coordinator")
_sys.modules[__name__] = _real
