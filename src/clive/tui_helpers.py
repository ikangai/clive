"""Compatibility shim — aliases tui.tui_helpers."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("tui.tui_helpers")
_sys.modules[__name__] = _real
