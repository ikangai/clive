"""Compatibility shim — aliases session.file_inspect."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("session.file_inspect")
_sys.modules[__name__] = _real
