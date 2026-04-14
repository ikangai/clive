"""Compatibility shim — aliases networking.lobby_connector."""
import importlib as _importlib
import sys as _sys
_real = _importlib.import_module("networking.lobby_connector")
_sys.modules[__name__] = _real
