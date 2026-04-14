"""Compatibility shim — aliases session.conv_loop."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("session.conv_loop")
_sys.modules[__name__] = _real
