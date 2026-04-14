"""Compatibility shim — aliases networking.lobby_client."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.lobby_client")
_sys.modules[__name__] = _real
