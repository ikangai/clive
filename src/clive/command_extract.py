"""Compatibility shim — aliases observation.command_extract."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("observation.command_extract")
_sys.modules[__name__] = _real
