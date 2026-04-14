"""Compatibility shim — aliases networking.lobby_server."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.lobby_server")
_sys.modules[__name__] = _real
