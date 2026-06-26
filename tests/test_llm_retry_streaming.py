"""Follow-up to test_llm_retry.py: extend bounded transient-retry to the paths
the first pass deferred — streaming (chat_stream) and the delegate / claude-cli
subprocess invokes.

Semantics under test:

  * chat_stream() retries the stream SETUP/connect on transient errors, but once
    tokens are flowing a mid-flight break surfaces as a CLEAN failure (it
    propagates) rather than silently re-running a half-consumed stream and
    yielding truncated/duplicated content.
  * _claude_cli_complete() retries a transient spawn failure (OSError — e.g.
    EAGAIN under load, or a transiently-missing binary) with bounded backoff, but
    a deterministic clean nonzero exit (bad arg / auth error) fails fast.
  * the delegate/stdio invoke retries a transient connection failure (broken pipe
    / reset) but fails fast on a deterministic delegate-side error.

The same _is_transient / _with_retry helpers from task-2ff77546 are reused — no
parallel predicate.
"""

import subprocess

import anthropic
import httpx
import openai
import pytest

import delegate_client
import llm


# ─── shared helpers ─────────────────────────────────────────────────────────

_REQ = httpx.Request("POST", "https://api.example.com/v1/chat/completions")


def _conn_error():
    return openai.APIConnectionError(message="connection reset", request=_REQ)


def _anthropic_conn_error():
    return anthropic.APIConnectionError(message="stream connect reset", request=_REQ)


def _auth_401():
    return openai.AuthenticationError(
        "boom", response=httpx.Response(401, request=_REQ), body=None
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make backoff sleeps instantaneous while recording how many happened."""
    calls = []
    monkeypatch.setattr(llm.time, "sleep", lambda d: calls.append(d))
    return calls


# ─── _is_transient: the new subprocess/stdio cases ──────────────────────────

def test_is_transient_true_for_spawn_oserror():
    assert llm._is_transient(BlockingIOError("EAGAIN: resource temporarily unavailable"))
    assert llm._is_transient(OSError("fork: cannot allocate memory"))
    assert llm._is_transient(ConnectionResetError("stdio pipe reset"))
    # Per task: a (transiently) missing binary on spawn is a retryable spawn error.
    assert llm._is_transient(FileNotFoundError("claude: not found"))


def test_is_transient_false_for_timeouts():
    # A request that ran out the clock must NOT be retried (re-running a
    # multi-minute call that already timed out is wasteful). TimeoutError
    # subclasses OSError, so it must be explicitly excluded.
    assert not llm._is_transient(TimeoutError("delegate timed out"))
    # subprocess timeout is not an OSError at all — still fail-fast.
    assert not llm._is_transient(subprocess.TimeoutExpired(cmd="claude", timeout=300))


# ─── chat_stream(): OpenAI-compatible path ──────────────────────────────────

class _Delta:
    def __init__(self, content):
        self.content = content


class _StreamChoice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content):
        self.choices = [_StreamChoice(content)]


class _StreamCompletions:
    def __init__(self, behavior):
        self._behavior = behavior
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs.get("stream") is True
        return self._behavior(self.calls)


class _StreamChat:
    def __init__(self, behavior):
        self.completions = _StreamCompletions(behavior)


class FakeOpenAIStreamClient:
    def __init__(self, behavior):
        self.chat = _StreamChat(behavior)


def test_openai_stream_setup_drops_once_then_yields_full_content(_no_sleep):
    def behavior(n):
        if n == 1:
            raise _conn_error()              # transient failure during SETUP
        return [_Chunk("Hello "), _Chunk("world")]

    client = FakeOpenAIStreamClient(behavior)
    tokens = []
    content, pt, ct = llm.chat_stream(
        client, [{"role": "user", "content": "hi"}],
        on_token=lambda t: tokens.append(t),
    )

    assert content == "Hello world"          # complete, not truncated
    assert tokens[-1] == "Hello world"
    assert client.chat.completions.calls == 2   # exactly one retry on setup
    assert len(_no_sleep) == 1


def test_openai_stream_setup_fails_fast_on_auth(_no_sleep):
    def behavior(n):
        raise _auth_401()

    client = FakeOpenAIStreamClient(behavior)
    with pytest.raises(openai.AuthenticationError):
        llm.chat_stream(client, [{"role": "user", "content": "hi"}])
    assert client.chat.completions.calls == 1   # NO retry on a 4xx
    assert _no_sleep == []


def test_openai_stream_midflight_break_is_clean_failure_not_retried(_no_sleep):
    seen = []

    def behavior(n):
        # Setup always succeeds; the stream breaks AFTER one token is delivered.
        def gen():
            yield _Chunk("partial ")
            raise _conn_error()
        return gen()

    client = FakeOpenAIStreamClient(behavior)
    with pytest.raises(openai.APIConnectionError):
        llm.chat_stream(
            client, [{"role": "user", "content": "hi"}],
            on_token=lambda t: seen.append(t),
        )
    # The half-consumed stream is NOT silently restarted — create() ran once.
    assert client.chat.completions.calls == 1
    assert _no_sleep == []
    # A token did flow before the break, but the function raised rather than
    # returning the truncated "partial " content.
    assert seen == ["partial "]


# ─── chat_stream(): Anthropic path ──────────────────────────────────────────

class _FakeFinalUsage:
    input_tokens = 11
    output_tokens = 7


class _FakeFinalMessage:
    usage = _FakeFinalUsage()


class _FakeAnthropicStream:
    def __init__(self, texts, break_after=None):
        self._texts = texts
        self._break_after = break_after

    @property
    def text_stream(self):
        for i, t in enumerate(self._texts):
            if self._break_after is not None and i == self._break_after:
                raise _anthropic_conn_error()
            yield t

    def get_final_message(self):
        return _FakeFinalMessage()


class _FakeStreamManager:
    def __init__(self, stream, raise_on_enter=None):
        self._stream = stream
        self._raise_on_enter = raise_on_enter
        self.exited = 0

    def __enter__(self):
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self._stream

    def __exit__(self, *exc):
        self.exited += 1
        return False


class _FakeMessages:
    def __init__(self, behavior):
        self._behavior = behavior
        self.stream_calls = 0

    def stream(self, **kwargs):
        self.stream_calls += 1
        return self._behavior(self.stream_calls)


def _fake_anthropic(behavior):
    client = anthropic.Anthropic(api_key="test-key")
    client.messages = _FakeMessages(behavior)
    return client


def test_anthropic_stream_setup_drops_once_then_yields_full_content(_no_sleep):
    def behavior(n):
        if n == 1:
            return _FakeStreamManager(None, raise_on_enter=_anthropic_conn_error())
        return _FakeStreamManager(_FakeAnthropicStream(["Hello ", "world"]))

    client = _fake_anthropic(behavior)
    content, pt, ct = llm.chat_stream(client, [{"role": "user", "content": "hi"}])

    assert content == "Hello world"
    assert (pt, ct) == (11, 7)               # final usage taken from full stream
    assert client.messages.stream_calls == 2   # one retry on setup
    assert len(_no_sleep) == 1


def test_anthropic_stream_midflight_break_is_clean_failure_not_retried(_no_sleep):
    managers = []

    def behavior(n):
        mgr = _FakeStreamManager(_FakeAnthropicStream(["partial ", "more"], break_after=1))
        managers.append(mgr)
        return mgr

    client = _fake_anthropic(behavior)
    with pytest.raises(anthropic.APIConnectionError):
        llm.chat_stream(client, [{"role": "user", "content": "hi"}])

    assert client.messages.stream_calls == 1   # not restarted mid-stream
    assert _no_sleep == []
    assert managers[0].exited == 1             # context cleaned up on the way out


# ─── _claude_cli_complete(): subprocess spawn retry ─────────────────────────

def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_claude_cli_retries_transient_spawn_then_succeeds(monkeypatch, _no_sleep):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BlockingIOError("Resource temporarily unavailable")
        return _completed(stdout='{"result": "hi from cli"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = llm.ClaudeCliClient()
    content, pt, ct = llm.chat(client, [{"role": "user", "content": "hi"}])

    assert content == "hi from cli"
    assert calls["n"] == 2                    # exactly one retry
    assert len(_no_sleep) == 1


def test_claude_cli_transient_spawn_is_bounded(monkeypatch, _no_sleep):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        raise BlockingIOError("persistently EAGAIN")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = llm.ClaudeCliClient()
    content, pt, ct = llm.chat(client, [{"role": "user", "content": "hi"}])

    # Bounded: no more than the configured attempt cap, then surfaced as a turn.
    assert calls["n"] == llm.RETRY_MAX_ATTEMPTS
    assert len(_no_sleep) == llm.RETRY_MAX_ATTEMPTS - 1
    assert "claude-cli error" in content


def test_claude_cli_clean_nonzero_exit_fails_fast(monkeypatch, _no_sleep):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        # A bad arg / auth error: the process RAN and exited nonzero — this is a
        # CompletedProcess, not an exception, so it must not be retried.
        return _completed(stderr="error: unknown option '--bogus'", returncode=2)

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = llm.ClaudeCliClient()
    content, pt, ct = llm.chat(client, [{"role": "user", "content": "hi"}])

    assert calls["n"] == 1                    # NO retry on a deterministic exit
    assert _no_sleep == []


def test_claude_cli_timeout_is_not_retried(monkeypatch, _no_sleep):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = llm.ClaudeCliClient()
    content, pt, ct = llm.chat(client, [{"role": "user", "content": "hi"}])

    assert calls["n"] == 1                    # a timeout is fail-fast, not transient
    assert _no_sleep == []
    assert "claude-cli error" in content


# ─── delegate / stdio invoke retry ──────────────────────────────────────────

def _delegate_response(content="ok", pt=3, ct=2):
    return delegate_client._ChatCompletion(
        choices=[delegate_client._Choice(message=delegate_client._Message(content=content))],
        usage=delegate_client._Usage(prompt_tokens=pt, completion_tokens=ct),
    )


def test_delegate_retries_transient_connection_then_succeeds(monkeypatch, _no_sleep):
    client = delegate_client.DelegateClient()
    calls = {"n": 0}

    def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("stdio pipe reset")
        return _delegate_response("delegated reply")

    monkeypatch.setattr(client.chat.completions, "create", fake_create)
    content, pt, ct = llm.chat(client, [{"role": "user", "content": "hi"}])

    assert content == "delegated reply"
    assert (pt, ct) == (3, 2)
    assert calls["n"] == 2                    # one retry on the transient reset
    assert len(_no_sleep) == 1


def test_delegate_fails_fast_on_deterministic_error(monkeypatch, _no_sleep):
    client = delegate_client.DelegateClient()
    calls = {"n": 0}

    def fake_create(**kwargs):
        calls["n"] += 1
        raise RuntimeError("delegate error: malformed request")

    monkeypatch.setattr(client.chat.completions, "create", fake_create)
    with pytest.raises(RuntimeError):
        llm.chat(client, [{"role": "user", "content": "hi"}])
    assert calls["n"] == 1                    # NO retry on a delegate-side error
    assert _no_sleep == []
