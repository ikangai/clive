"""Compatibility shim — aliases evolution.evolve."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("evolution.evolve")
_sys.modules[__name__] = _real
