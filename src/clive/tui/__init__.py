"""tui subpackage — re-exports tui.tui as the package itself."""
import importlib as _importlib
import sys as _sys

_real = _importlib.import_module(".tui", __name__)
_pkg_name = __name__
_sys.modules[_pkg_name] = _real
_real.__name__ = _pkg_name
_real.__path__ = [__path__[0]] if isinstance(__path__, list) else list(__path__)
_real.__package__ = _pkg_name
