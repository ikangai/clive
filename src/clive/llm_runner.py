"""Compatibility shim — aliases execution.llm_runner."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("execution.llm_runner")
_sys.modules[__name__] = _real
