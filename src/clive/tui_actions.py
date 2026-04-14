"""Compatibility shim — aliases tui.tui_actions."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("tui.tui_actions")
_sys.modules[__name__] = _real
