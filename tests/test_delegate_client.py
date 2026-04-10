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
    client = llm.get_client()
    assert isinstance(client, DelegateClient)
