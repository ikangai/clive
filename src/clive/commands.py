"""Compatibility shim — aliases session.commands."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("session.commands")
_sys.modules[__name__] = _real
