"""Compatibility shim — aliases planning.summarizer."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("planning.summarizer")
_sys.modules[__name__] = _real
