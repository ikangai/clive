"""Tests for bounded exponential-backoff retry of transient LLM errors.

llm.chat() and llm.chat_with_tools() each make a single non-streaming provider
call. A transient 429 / 5xx / connection-reset used to fail the whole subtask
on the first try. _with_retry() wraps those NON-STREAMING call sites with a
capped, jittered exponential backoff that retries ONLY transient errors and
fails fast on 4xx auth/validation errors. Streaming and delegate/claude-cli
subprocess reconnection are intentionally out of scope here.
"""

import httpx
import openai
import pytest

import llm


# ─── helpers ────────────────────────────────────────────────────────────────

_REQ = httpx.Request("POST", "https://api.example.com/v1/chat/completions")


def _err(cls, status):
    """Build a real openai SDK status error carrying *status*."""
    return cls("boom", response=httpx.Response(status, request=_REQ), body=None)


def _rate_limit():
    return _err(openai.RateLimitError, 429)


def _server_503():
    return _err(openai.InternalServerError, 503)


def _auth_401():
    return _err(openai.AuthenticationError, 401)


def _bad_request_400():
    return _err(openai.BadRequestError, 400)


def _conn_error():
    return openai.APIConnectionError(message="connection reset", request=_REQ)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make backoff sleeps instantaneous so the suite stays fast, while still
    recording how many times (and how long) we backed off."""
    calls = []
    monkeypatch.setattr(llm.time, "sleep", lambda d: calls.append(d))
    return calls


# ─── _with_retry unit behaviour ─────────────────────────────────────────────

def test_with_retry_returns_after_one_transient_then_success(_no_sleep):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _rate_limit()
        return "ok"

    assert llm._with_retry(call) == "ok"
    assert attempts["n"] == 2          # exactly one retry
    assert len(_no_sleep) == 1         # backed off exactly once


def test_with_retry_treats_503_as_transient(_no_sleep):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _server_503()
        return "ok"

    assert llm._with_retry(call) == "ok"
    assert attempts["n"] == 2


def test_with_retry_treats_connection_error_as_transient(_no_sleep):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _conn_error()
        return "ok"

    assert llm._with_retry(call) == "ok"
    assert attempts["n"] == 2


def test_with_retry_fails_fast_on_auth_error(_no_sleep):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise _auth_401()

    with pytest.raises(openai.AuthenticationError):
        llm._with_retry(call)
    assert attempts["n"] == 1          # NO retry
    assert _no_sleep == []             # never backed off


def test_with_retry_fails_fast_on_bad_request(_no_sleep):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise _bad_request_400()

    with pytest.raises(openai.BadRequestError):
        llm._with_retry(call)
    assert attempts["n"] == 1


def test_with_retry_never_exceeds_attempt_cap(_no_sleep):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise _rate_limit()            # persistently transient

    with pytest.raises(openai.RateLimitError):
        llm._with_retry(call)
    assert attempts["n"] == llm.RETRY_MAX_ATTEMPTS
    assert attempts["n"] <= 3          # cap is small/bounded
    # one fewer sleep than attempts (no sleep after the final failure)
    assert len(_no_sleep) == llm.RETRY_MAX_ATTEMPTS - 1


def test_with_retry_backoff_is_bounded(_no_sleep):
    def call():
        raise _rate_limit()

    with pytest.raises(openai.RateLimitError):
        llm._with_retry(call)
    # every backoff delay is finite and within the configured ceiling (+jitter)
    assert all(0 <= d <= llm.RETRY_MAX_DELAY * 2 for d in _no_sleep)


# ─── end-to-end through chat() / chat_with_tools() ──────────────────────────

class _FakeUsage:
    prompt_tokens = 11
    completion_tokens = 7


class _FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, content, tool_calls=None):
        self.message = _FakeMessage(content, tool_calls)


class _FakeResponse:
    def __init__(self, content, tool_calls=None):
        self.choices = [_FakeChoice(content, tool_calls)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, behavior):
        self._behavior = behavior
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self._behavior(self.calls)


class _FakeChat:
    def __init__(self, behavior):
        self.completions = _FakeCompletions(behavior)


class FakeOpenAIClient:
    """Duck-typed openai-compatible client — not an anthropic/Delegate/CLI
    client, so chat() routes it through the OpenAI-compatible path."""

    def __init__(self, behavior):
        self.chat = _FakeChat(behavior)


def test_chat_retries_transient_then_returns_content(_no_sleep):
    def behavior(n):
        if n == 1:
            raise _rate_limit()
        return _FakeResponse("hello after retry")

    client = FakeOpenAIClient(behavior)
    content, pt, ct = llm.chat(client, [{"role": "user", "content": "hi"}])
    assert content == "hello after retry"
    assert (pt, ct) == (11, 7)
    assert client.chat.completions.calls == 2   # exactly one retry


def test_chat_fails_fast_on_auth_error(_no_sleep):
    def behavior(n):
        raise _auth_401()

    client = FakeOpenAIClient(behavior)
    with pytest.raises(openai.AuthenticationError):
        llm.chat(client, [{"role": "user", "content": "hi"}])
    assert client.chat.completions.calls == 1   # NO retry
    assert _no_sleep == []


def test_chat_with_tools_retries_transient_then_returns(_no_sleep):
    def behavior(n):
        if n == 1:
            raise _server_503()
        return _FakeResponse("tooled", tool_calls=[])

    client = FakeOpenAIClient(behavior)
    raw, text, pt, ct = llm.chat_with_tools(
        client, [{"role": "user", "content": "hi"}], tools=[]
    )
    assert text == "tooled"
    assert client.chat.completions.calls == 2
