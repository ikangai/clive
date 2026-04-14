"""Compatibility shim — aliases evolution.evolve_fitness."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("evolution.evolve_fitness")
_sys.modules[__name__] = _real
