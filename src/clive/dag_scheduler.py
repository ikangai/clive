"""Compatibility shim — aliases planning.dag_scheduler."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("planning.dag_scheduler")
_sys.modules[__name__] = _real
