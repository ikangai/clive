"""Compatibility shim — aliases tui.tui_task_runner."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("tui.tui_task_runner")
_sys.modules[__name__] = _real
