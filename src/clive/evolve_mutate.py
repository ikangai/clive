"""Compatibility shim — aliases evolution.evolve_mutate."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("evolution.evolve_mutate")
_sys.modules[__name__] = _real
