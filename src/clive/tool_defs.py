"""Compatibility shim — aliases llm.tool_defs."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("llm.tool_defs")
_sys.modules[__name__] = _real
