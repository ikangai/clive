"""Compatibility shim — aliases observation.context_compress."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("observation.context_compress")
_sys.modules[__name__] = _real
