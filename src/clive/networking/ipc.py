"""Cross-process SharedBrain via Unix domain socket.

The first clive instance starts the server; subsequent instances connect as clients.
Uses a simple JSON-line protocol over Unix domain sockets.
"""

import json
import logging
import os
import socket
import socketserver
import threading
import time

log = logging.getLogger(__name__)


class SharedBrainServer:
    """Unix domain socket server that holds the shared brain state."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._facts: list[dict] = []
        self._messages: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._server = None

    def serve(self):
        """Start serving. Blocks until shutdown() is called."""
        if os.path.exists(self.socket_path):
            # Check if an existing server is live before removing
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.connect(self.socket_path)
                probe.close()
                raise RuntimeError(f"SharedBrain server already running on {self.socket_path}")
            except ConnectionRefusedError:
                os.unlink(self.socket_path)  # stale socket, safe to remove

        self._server = _BrainSocketServer(
            self.socket_path, self._handle_request, self._lock,
            self._facts, self._messages
        )
        log.info("SharedBrain server listening on %s", self.socket_path)
        self._server.serve_forever()

    def shutdown(self):
        if self._server:
            self._server.shutdown()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def _handle_request(self, data: dict) -> dict:
        action = data.get("action")
        if action == "post_fact":
            with self._lock:
                self._facts.append({
                    "agent": data.get("agent", ""),
                    "fact": data.get("fact", ""),
                    "time": time.time(),
                })
            return {"ok": True}
        elif action == "get_facts":
            with self._lock:
                return {"ok": True, "data": list(self._facts)}
        elif action == "send_message":
            with self._lock:
                to = data.get("to_agent", "")
                if to not in self._messages:
                    self._messages[to] = []
                self._messages[to].append({
                    "from": data.get("from_agent", ""),
                    "message": data.get("message", ""),
                    "time": time.time(),
                })
            return {"ok": True}
        elif action == "get_messages":
            with self._lock:
                agent = data.get("agent", "")
                msgs = self._messages.pop(agent, [])
            return {"ok": True, "data": msgs}
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}


class _BrainRequestHandler(socketserver.StreamRequestHandler):
    """Handle a single client connection -- reads JSON lines, writes responses."""

    def handle(self):
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                result = self.server.handle_brain_request(data)
                self.wfile.write(json.dumps(result).encode() + b"\n")
                self.wfile.flush()
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                resp = json.dumps({"ok": False, "error": str(e)})
                self.wfile.write(resp.encode() + b"\n")
                self.wfile.flush()


class _BrainSocketServer(socketserver.ThreadingUnixStreamServer):
    """Threaded Unix socket server with brain state."""

    def __init__(self, socket_path, handler_func, lock, facts, messages):
        self.handle_brain_request = handler_func
        self._lock = lock
        self._facts = facts
        self._messages = messages
        super().__init__(socket_path, _BrainRequestHandler)


class SharedBrainClient:
    """Client that connects to a SharedBrainServer."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(socket_path)
        self._rfile = self._sock.makefile("rb")
        self._wfile = self._sock.makefile("wb")
        self._lock = threading.Lock()

    def post_fact(self, agent: str, fact: str) -> bool:
        resp = self._request({"action": "post_fact", "agent": agent, "fact": fact})
        return resp.get("ok", False)

    def get_facts(self) -> list[dict]:
        resp = self._request({"action": "get_facts"})
        return resp.get("data", [])

    def send_message(self, from_agent: str, to_agent: str, message: str) -> bool:
        resp = self._request({
            "action": "send_message",
            "from_agent": from_agent,
            "to_agent": to_agent,
            "message": message,
        })
        return resp.get("ok", False)

    def get_messages(self, agent: str) -> list[dict]:
        resp = self._request({"action": "get_messages", "agent": agent})
        return resp.get("data", [])

    def close(self):
        try:
            self._rfile.close()
        except OSError:
            pass
        try:
            self._wfile.close()
        except OSError:
            pass
        self._sock.close()

    def _request(self, data: dict) -> dict:
        with self._lock:
            msg = json.dumps(data).encode() + b"\n"
            self._wfile.write(msg)
            self._wfile.flush()
            line = self._rfile.readline()
            if not line:
                return {"ok": False, "error": "Connection closed"}
            return json.loads(line)


def connect_or_serve(socket_path: str) -> tuple[SharedBrainClient | None, SharedBrainServer | None]:
    """Try to connect to an existing server; if none exists, return a server to start.

    Returns (client, None) if a server is already running,
    or (None, server) if a new server should be started.
    """
    if os.path.exists(socket_path):
        try:
            client = SharedBrainClient(socket_path)
            return client, None
        except (ConnectionRefusedError, OSError):
            # Stale socket
            try:
                os.unlink(socket_path)
            except OSError:
                pass
    return None, SharedBrainServer(socket_path)
