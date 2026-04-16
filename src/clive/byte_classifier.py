"""Compatibility shim — aliases observation.byte_classifier."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("observation.byte_classifier")
_sys.modules[__name__] = _real
