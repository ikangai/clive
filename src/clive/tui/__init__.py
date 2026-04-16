"""tui subpackage — re-exports tui.tui as the package itself."""
import importlib, sys
_real = importlib.import_module(".tui", __name__)
_real.__name__ = __name__
_real.__path__ = list(__path__)
_real.__package__ = __name__
sys.modules[__name__] = _real
