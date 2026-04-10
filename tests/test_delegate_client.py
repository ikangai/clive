"""Tests for DelegateClient — the remote-side stdio LLM client.

DelegateClient translates an openai.ChatCompletion call into a pair of
framed protocol messages (llm_request → llm_response/llm_error) on
stdio. The outer clive detects the request frame in the pane, answers
it with its own LLM, and types the response frame into the pane.

These tests do NOT use real stdin/stdout — they inject StringIO
buffers so each test is deterministic and fast.
"""
import io

import pytest

from protocol import encode, decode_all
from delegate_client import DelegateClient


def test_chat_completion_round_trip():
    """DelegateClient writes an llm_request frame and reads an llm_response."""
    out_buf = io.StringIO()
    in_buf = io.StringIO()

    # Pre-seed the response the caller will "send back".
    in_buf.write(encode("llm_response", {
        "id": "test-001",
        "content": "42",
        "prompt_tokens": 10,
        "completion_tokens": 2,
    }, nonce="") + "\n")
    in_buf.seek(0)

    client = DelegateClient(stdout=out_buf, stdin=in_buf, poll_interval=0.01)
    client._new_id = lambda: "test-001"

    resp = client.chat.completions.create(
        model="delegate",
        messages=[{"role": "user", "content": "What is 6*7?"}],
        max_tokens=16,
    )

    # Outgoing frame should be an llm_request with id=test-001
    frames = decode_all(out_buf.getvalue())
    req = [f for f in frames if f.kind == "llm_request"]
    assert len(req) == 1
    assert req[0].payload["id"] == "test-001"
    assert req[0].payload["messages"] == [{"role": "user", "content": "What is 6*7?"}]
    assert req[0].payload["max_tokens"] == 16

    # Response shape mirrors openai.ChatCompletion for llm.chat() consumers
    assert resp.choices[0].message.content == "42"
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 2


def test_error_frame_raises():
    out_buf = io.StringIO()
    in_buf = io.StringIO()
    in_buf.write(encode("llm_error", {
        "id": "test-002",
        "error": "outer LLM unreachable",
    }, nonce="") + "\n")
    in_buf.seek(0)

    client = DelegateClient(stdout=out_buf, stdin=in_buf, poll_interval=0.01)
    client._new_id = lambda: "test-002"

    with pytest.raises(RuntimeError, match="outer LLM unreachable"):
        client.chat.completions.create(
            model="delegate",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16,
        )


def test_ignores_mismatched_response_id():
    """Stale response from a previous request must not be consumed."""
    out_buf = io.StringIO()
    in_buf = io.StringIO()

    # Seed a stale response with the WRONG id, then the real one.
    in_buf.write(encode("llm_response", {
        "id": "stale-000", "content": "old", "prompt_tokens": 0, "completion_tokens": 0,
    }, nonce="") + "\n")
    in_buf.write(encode("llm_response", {
        "id": "test-003", "content": "new", "prompt_tokens": 0, "completion_tokens": 0,
    }, nonce="") + "\n")
    in_buf.seek(0)

    client = DelegateClient(stdout=out_buf, stdin=in_buf, poll_interval=0.01)
    client._new_id = lambda: "test-003"
    resp = client.chat.completions.create(
        model="delegate", messages=[], max_tokens=1,
    )
    assert resp.choices[0].message.content == "new"


def test_honours_env_nonce_on_outgoing_frame(monkeypatch):
    """Production inner clive has CLIVE_FRAME_NONCE set; the request frame
    must carry that nonce so the outer's reader accepts it."""
    monkeypatch.setenv("CLIVE_FRAME_NONCE", "inner-nonce")
    out_buf = io.StringIO()
    in_buf = io.StringIO()
    in_buf.write(encode("llm_response", {
        "id": "nid-1", "content": "ok", "prompt_tokens": 0, "completion_tokens": 0,
    }, nonce="inner-nonce") + "\n")
    in_buf.seek(0)

    client = DelegateClient(stdout=out_buf, stdin=in_buf, poll_interval=0.01)
    client._new_id = lambda: "nid-1"
    client.chat.completions.create(
        model="delegate", messages=[], max_tokens=1,
    )

    # Outgoing frame must carry the nonce
    assert "<<<CLIVE:llm_request:inner-nonce:" in out_buf.getvalue()


def test_llm_chat_routes_through_delegate_client(monkeypatch):
    """llm.chat() with a DelegateClient instance must produce the same
    (content, prompt_tokens, completion_tokens) tuple as with a real
    openai client — the drop-in shape matters."""
    import io

    out_buf = io.StringIO()
    in_buf = io.StringIO()
    in_buf.write(encode("llm_response", {
        "id": "rt-1", "content": "hi back",
        "prompt_tokens": 4, "completion_tokens": 2,
    }, nonce="") + "\n")
    in_buf.seek(0)

    client = DelegateClient(stdout=out_buf, stdin=in_buf, poll_interval=0.01)
    client._new_id = lambda: "rt-1"

    import llm
    content, pt, ct = llm.chat(
        client,
        [{"role": "user", "content": "hi"}],
        max_tokens=16,
    )
    assert content == "hi back"
    assert pt == 4
    assert ct == 2


def test_get_client_returns_delegate_when_provider_is_delegate(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "delegate")
    import importlib, llm
    importlib.reload(llm)
    try:
        client = llm.get_client()
        assert isinstance(client, DelegateClient)
    finally:
        # Reset module state so later tests that import llm see a fresh
        # _client_cache and the original PROVIDER_NAME. Without this,
        # stale delegate state leaks across the test suite.
        llm._client_cache = None
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        importlib.reload(llm)


# ─── Liveness: timeout must actually fire when the outer is silent ──────────

def test_timeout_fires_when_outer_never_responds():
    """Regression test for C1.

    Previously, DelegateClient._read_available() called readline() on
    real stdin without any readability check. If the outer crashed or
    hung, readline() blocked indefinitely, the loop's deadline check
    never ran, and the 300s timeout never fired — the inner clive
    hung forever.

    Fix: _read_available() now uses select.select() with poll_interval
    to check readability before reading. A stuck outer no longer
    wedges the inner; the TimeoutError arrives on schedule.
    """
    import subprocess
    import sys
    import time
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    child_code = (
        "import os\n"
        "os.environ.setdefault('CLIVE_FRAME_NONCE', '')\n"
        "import sys, time\n"
        "sys.path.insert(0, '.')\n"
        "from delegate_client import DelegateClient\n"
        "client = DelegateClient(stdout=sys.stdout, stdin=sys.stdin,\n"
        "                        poll_interval=0.05, timeout=1.5)\n"
        "start = time.time()\n"
        "try:\n"
        "    client.chat_completions_create(\n"
        "        model='x',\n"
        "        messages=[{'role': 'user', 'content': 'hi'}],\n"
        "        max_tokens=1,\n"
        "    )\n"
        "    print(f'UNEXPECTED_SUCCESS after {time.time()-start:.2f}s', flush=True)\n"
        "except TimeoutError as e:\n"
        "    print(f'TIMEOUT after {time.time()-start:.2f}s', flush=True)\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", child_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(repo_root),
        text=True,
        bufsize=0,
    )

    start = time.time()
    try:
        # Give the child up to 5s to hit its 1.5s internal timeout.
        # If the fix is in place we expect ~1.5-2.0s wall time.
        rc = proc.wait(timeout=5)
        elapsed = time.time() - start
        stdout = proc.stdout.read()
        stderr = proc.stderr.read()
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(
            "DelegateClient did not fire its internal TimeoutError "
            "within 5s — the outer-hang liveness bug has regressed. "
            "See C1 in the Phase 2 review."
        )
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass

    assert rc == 0, f"child exited rc={rc}, stderr={stderr!r}"
    assert "TIMEOUT" in stdout, (
        f"Expected TimeoutError, got stdout={stdout!r} stderr={stderr!r}"
    )
    # The internal timeout was 1.5s; allow some slack for process startup.
    assert elapsed < 4.0, (
        f"Timeout took {elapsed:.2f}s — should be ~1.5s. Is select actually "
        f"polling, or did it fall back to blocking readline?"
    )
