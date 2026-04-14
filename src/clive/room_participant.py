"""Compatibility shim — aliases execution.room_participant."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("execution.room_participant")
_sys.modules[__name__] = _real
