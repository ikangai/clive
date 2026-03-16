"""Output routing for clive.

Separates telemetry (progress) from results:
- Normal mode: both go to stdout
- Quiet mode (--quiet): telemetry to stderr, results to stdout

This enables clive as a shell primitive:
    result=$(clive --quiet "task")   # captures only the result
"""
import sys

_quiet = False


def set_quiet(quiet: bool):
    """Enable/disable quiet mode."""
    global _quiet
    _quiet = quiet


def is_quiet() -> bool:
    """Check if quiet mode is active."""
    return _quiet


def progress(msg: str):
    """Print progress/telemetry. Goes to stderr in quiet mode."""
    print(msg, file=sys.stderr if _quiet else sys.stdout)


def result(msg: str):
    """Print final result. Always goes to stdout."""
    print(msg, file=sys.stdout)
