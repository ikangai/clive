"""llm subpackage — re-exports llm.llm as the package itself."""
import importlib as _importlib
import sys as _sys

# Load the actual llm.llm module and make it the package
_real = _importlib.import_module(".llm", __name__)

# Expose all attributes from llm.llm at the package level
# so `from llm import chat` and `import llm; llm.chat` both work
_pkg_name = __name__
_sys.modules[_pkg_name] = _real
# Keep the package's __name__ and __path__ so submodule imports still work
_real.__name__ = _pkg_name
_real.__path__ = [__path__[0]] if isinstance(__path__, list) else list(__path__)
_real.__package__ = _pkg_name
