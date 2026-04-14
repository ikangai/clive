"""Compatibility shim — aliases llm.delegate_client."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("llm.delegate_client")
_sys.modules[__name__] = _real
