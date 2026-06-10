"""tmux control-mode sidecar for event-driven scheduling (gh#12).

One `tmux -C attach -r` process per clive session yields machine-readable
notifications (%output, %window-close, ...) for EVERY pane through a
single connection — no per-pane capture_pane polling. The sidecar parses
the stream and fans events out to subscribers:

    sc = ControlSidecar(session_name="clive")
    q = sc.subscribe("%42")          # per-pane event queue
    sc.on_any(callback)              # all events
    sc.wake_on_output(wake_event)    # set a threading.Event on any %output
    sc.start()                       # spawn the attach process + reader
    ...
    sc.stop()

Scope notes:
- The attach is read-only (`-r`) and on its own client; it never types.
- %output payloads arrive octal-escaped (\\015 etc.) — unescaped here.
- Lines between %begin/%end are command replies, not notifications;
  they are ignored, as is anything not starting with '%'.
- Unknown %-notifications pass through with kind="raw" so callers can
  observe tmux-version-specific events without parser churn.

This complements (not replaces) the pipe-pane FIFO streaming path
(observation/fifo_stream.py): the FIFO carries one pane's bytes for the
byte-classifier; the sidecar carries session-wide lifecycle events for
the scheduler. Wired into planning/dag_scheduler.py behind
CLIVE_CONTROL_SIDECAR=1 (default off).
"""
from __future__ import annotations

import queue
import re
import subprocess
import threading
from dataclasses import dataclass

_OCTAL_RE = re.compile(r"\\([0-7]{3})")

# Notifications that carry a pane id as their first token.
_KNOWN_KINDS = {
    "%output": "output",
    "%exit": "exit",
    "%window-close": "window-close",
    "%unlinked-window-close": "window-close",
    "%session-changed": "session-changed",
    "%window-pane-changed": "window-pane-changed",
    "%layout-change": "layout-change",
}

# Command-reply framing — not notifications.
_REPLY_MARKERS = ("%begin", "%end", "%error")


@dataclass(frozen=True)
class ControlEvent:
    kind: str
    pane_id: str | None = None
    data: str = ""


def unescape_output(text: str) -> str:
    """Undo tmux control-mode octal escaping (\\015 -> CR, \\\\ -> \\)."""
    # \\ first would corrupt octals; do octals on a split-by-backslash-pair
    parts = text.split("\\\\")
    parts = [_OCTAL_RE.sub(lambda m: chr(int(m.group(1), 8)), p) for p in parts]
    return "\\".join(parts)


def parse_control_line(line: str) -> ControlEvent | None:
    """Parse one control-mode stdout line into a ControlEvent.

    Returns None for command-reply framing and non-notification lines.
    """
    if not line.startswith("%"):
        return None
    token = line.split(" ", 1)[0]
    if token in _REPLY_MARKERS:
        return None

    if token == "%output":
        # %output %<pane-id> <octal-escaped data>
        rest = line[len("%output "):]
        pane_id, _, payload = rest.partition(" ")
        return ControlEvent(
            kind="output", pane_id=pane_id, data=unescape_output(payload)
        )

    kind = _KNOWN_KINDS.get(token)
    rest = line[len(token):].strip()
    if kind is None:
        return ControlEvent(kind="raw", data=line)
    if kind == "exit":
        return ControlEvent(kind="exit", data=rest)
    return ControlEvent(kind=kind, data=rest)


class ControlSidecar:
    """Reader thread around `tmux -C attach-session -r`.

    Thread-safe for subscribe/on_any/wake_on_output before or after
    start(). Dispatch never raises: a failing callback is isolated so
    one bad subscriber cannot stall the event stream.
    """

    def __init__(self, session_name: str, socket_name: str | None = None):
        self.session_name = session_name
        self.socket_name = socket_name
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pane_queues: dict[str, list[queue.Queue]] = {}
        self._any_callbacks: list = []
        self._wake_events: list[threading.Event] = []
        self._stopped = threading.Event()

    # -- subscription surface ------------------------------------------

    def subscribe(self, pane_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._pane_queues.setdefault(pane_id, []).append(q)
        return q

    def on_any(self, callback) -> None:
        with self._lock:
            self._any_callbacks.append(callback)

    def wake_on_output(self, event: threading.Event) -> None:
        with self._lock:
            self._wake_events.append(event)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        cmd = ["tmux"]
        if self.socket_name:
            cmd += ["-L", self.socket_name]
        cmd += ["-C", "attach-session", "-r", "-t", self.session_name]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._thread = threading.Thread(
            target=self._read_loop, name="control-sidecar", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # -- internals ------------------------------------------------------

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            if self._stopped.is_set():
                break
            self._dispatch_line(line.rstrip("\n"))

    def _dispatch_line(self, line: str) -> None:
        ev = parse_control_line(line)
        if ev is None:
            return
        with self._lock:
            callbacks = list(self._any_callbacks)
            pane_queues = list(self._pane_queues.get(ev.pane_id or "", ()))
            wakes = list(self._wake_events) if ev.kind == "output" else ()
        for cb in callbacks:
            try:
                cb(ev)
            except Exception:
                pass
        for q in pane_queues:
            q.put(ev)
        for w in wakes:
            w.set()
