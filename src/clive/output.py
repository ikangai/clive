"""Output routing for clive.

Separates telemetry (progress) from results:
- Normal mode: both go to stdout
- Quiet mode (--quiet): telemetry to stderr, results to stdout

This enables clive as a shell primitive:
    result=$(clive --quiet "task")   # captures only the result
"""
import sys
import threading
import time

from protocol import encode

_quiet = False
_conversational = False
_lock = threading.RLock()  # RLock (not Lock) so signal handlers can re-enter from the main thread mid-`with _lock:` block — see Bug H9, 2026-05-20 debug session.
_active = None  # Current pulsating _Pulse instance or None

# Blue pulse cycle (similar to Claude Code)
_PULSE_COLORS = [
    "\033[38;5;63m",   # slate blue
    "\033[38;5;69m",   # cornflower
    "\033[38;5;75m",   # sky blue
    "\033[38;5;81m",   # light cyan
    "\033[38;5;75m",   # sky blue
    "\033[38;5;69m",   # cornflower
]
_RESET = "\033[0m"


def _stream():
    return sys.stderr if _quiet else sys.stdout


def _is_tty():
    s = _stream()
    return hasattr(s, "isatty") and s.isatty()


class _Pulse:
    """Background thread that animates a symbol's color on the current terminal line."""

    def __init__(self, symbol, text, stream, indent=""):
        self.symbol = symbol
        self.text = text
        self.stream = stream
        self.indent = indent
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        i = 0
        while not self._stop.wait(0.12):
            c = _PULSE_COLORS[i % len(_PULSE_COLORS)]
            try:
                self.stream.write(f"\r{self.indent}{c}{self.symbol}{_RESET} {self.text}\033[K")
                self.stream.flush()
            except (OSError, ValueError):
                break
            i += 1

    def _stop_and_write(self, text: str):
        self._stop.set()
        self._thread.join(timeout=1)
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def finalize(self):
        """Stop animation and write final static line."""
        self._stop_and_write(f"\r{self.indent}{self.symbol} {self.text}\033[K\n")

    def replace(self, full_line):
        """Stop animation and replace this line with different content."""
        self._stop_and_write(f"\r{full_line}\033[K\n")


def _stop_active():
    """Stop any active pulse, finalizing its line. Must hold _lock."""
    global _active
    if _active:
        _active.finalize()
        _active = None


# --- Public API ---

def set_quiet(quiet: bool):
    """Enable/disable quiet mode."""
    global _quiet
    _quiet = quiet


def set_conversational(enabled: bool):
    """Enable/disable conversational output mode (clive-to-clive)."""
    global _conversational
    _conversational = enabled


def is_conversational() -> bool:
    """Check if conversational mode is active."""
    return _conversational


def progress(msg: str):
    """Legacy progress output. Stops any active animation first."""
    if _conversational: emit_progress(msg); return
    with _lock:
        _stop_active()
    print(msg, file=_stream())


def step(msg: str):
    """Major step marker with pulsating ⏺."""
    if _conversational: emit_progress(msg); return
    global _active
    with _lock:
        _stop_active()
        s = _stream()
        if _is_tty():
            s.write("\n")
            s.flush()
            _active = _Pulse("⏺", msg, s)
        else:
            print(f"\n⏺ {msg}", file=s)


def detail(msg: str):
    """Indented detail line. Replaces any active activity pulse."""
    if _conversational: emit_progress(msg); return
    global _active
    with _lock:
        if _active and _active.indent:
            _active.replace(f"  {msg}")
            _active = None
        else:
            _stop_active()
            print(f"  {msg}", file=_stream())


def activity(msg: str):
    """In-progress activity line with pulsating ◌ indicator."""
    if _conversational: emit_progress(msg); return
    global _active
    with _lock:
        _stop_active()
        s = _stream()
        if _is_tty():
            _active = _Pulse("◌", msg, s, indent="  ")
        else:
            print(f"  ◌ {msg}", file=s)


def finish():
    """Stop any active animation. Call at end of program."""
    with _lock:
        _stop_active()


def result(msg: str):
    """Print final result. Always goes to stdout."""
    with _lock:
        _stop_active()
    print(msg, file=sys.stdout)


# --- Conversational protocol ---

def emit_turn(state: str):
    """Emit a framed turn-state message. States: thinking, waiting, done, failed."""
    print(encode("turn", {"state": state}), flush=True)


def emit_context(data: dict):
    """Emit a framed context message with an arbitrary JSON-serializable dict."""
    print(encode("context", data), flush=True)


def emit_question(question: str):
    """Emit a framed question message."""
    print(encode("question", {"text": question}), flush=True)


def emit_file(name: str):
    """Emit a framed file-available message."""
    print(encode("file", {"name": name}), flush=True)


def emit_progress(text: str):
    """Emit a framed progress message."""
    print(encode("progress", {"text": text}), flush=True)


def emit_alive():
    """Emit a framed keepalive message with current wall-clock timestamp."""
    print(encode("alive", {"ts": time.time()}), flush=True)
