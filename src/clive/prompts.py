"""Compatibility shim — aliases llm.prompts."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("llm.prompts")
_sys.modules[__name__] = _real
