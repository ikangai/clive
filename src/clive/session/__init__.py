"""session subpackage."""
from .session import *  # noqa: F401,F403
# Explicit re-export for helpers that star-import skips (underscore-prefixed
# or not explicitly listed). The streaming-observation lifecycle helpers
# are consumed by tests and future runners.
from .session import _maybe_attach_stream, detach_stream  # noqa: F401
