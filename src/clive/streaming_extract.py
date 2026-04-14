"""Compatibility shim — aliases observation.streaming_extract."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("observation.streaming_extract")
_sys.modules[__name__] = _real
