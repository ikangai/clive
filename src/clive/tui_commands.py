"""Compatibility shim — aliases tui.tui_commands."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("tui.tui_commands")
_sys.modules[__name__] = _real
