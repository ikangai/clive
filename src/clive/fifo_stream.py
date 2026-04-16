"""Compatibility shim — aliases observation.fifo_stream."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("observation.fifo_stream")
_sys.modules[__name__] = _real
