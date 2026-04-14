"""Compatibility shim — aliases session.toolsets."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("session.toolsets")
_sys.modules[__name__] = _real
