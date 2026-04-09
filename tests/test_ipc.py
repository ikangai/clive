# tests/test_ipc.py
import os
import tempfile
import threading
import time
from ipc import SharedBrainServer, SharedBrainClient


def _short_sock_path():
    """Create a short socket path (macOS AF_UNIX limit is 104 chars)."""
    fd, path = tempfile.mkstemp(prefix="brain", suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(path)
    return path


def test_server_starts_and_stops(tmp_path):
    socket_path = _short_sock_path()
    server = SharedBrainServer(socket_path)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    time.sleep(0.2)
    server.shutdown()
    t.join(timeout=5)

def test_cross_process_fact_sharing(tmp_path):
    socket_path = _short_sock_path()
    server = SharedBrainServer(socket_path)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    time.sleep(0.2)

    client1 = SharedBrainClient(socket_path)
    client2 = SharedBrainClient(socket_path)

    client1.post_fact("weather", "sunny")
    facts = client2.get_facts()
    assert any(f["fact"] == "sunny" for f in facts)

    server.shutdown()
    t.join(timeout=5)

def test_cross_process_messaging(tmp_path):
    socket_path = _short_sock_path()
    server = SharedBrainServer(socket_path)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    time.sleep(0.2)

    client1 = SharedBrainClient(socket_path)
    client2 = SharedBrainClient(socket_path)

    client1.send_message("agent-a", "agent-b", "hello from a")
    msgs = client2.get_messages("agent-b")
    assert len(msgs) == 1
    assert msgs[0]["message"] == "hello from a"

    # Messages should be consumed (cleared after get)
    msgs2 = client2.get_messages("agent-b")
    assert len(msgs2) == 0

    server.shutdown()
    t.join(timeout=5)

def test_multiple_facts(tmp_path):
    socket_path = _short_sock_path()
    server = SharedBrainServer(socket_path)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    time.sleep(0.2)

    client = SharedBrainClient(socket_path)
    client.post_fact("agent1", "fact one")
    client.post_fact("agent2", "fact two")
    facts = client.get_facts()
    assert len(facts) >= 2

    server.shutdown()
    t.join(timeout=5)
