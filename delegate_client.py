"""Delegate LLM client — routes inference requests over stdio to the outer clive.

When a remote clive is configured with ``LLM_PROVIDER=delegate``, every
chat completion is serialized as an ``llm_request`` frame on stdout
and blocks reading stdin until a matching ``llm_response`` (or
``llm_error``) frame arrives. The outer clive is responsible for
detecting ``llm_request`` frames in the pane, answering them with its
own (local) LLM, and typing the response frame into the pane via
tmux send-keys.

This eliminates the "LMStudio is on the caller's localhost but the
remote clive tries to hit ITS OWN localhost" failure mode that happens
with the legacy SendEnv-only approach, without any network tunneling.

Thread safety: not concurrent. If the inner's planner/executor ever
starts issuing simultaneous chat calls on the same client, add a
threading.Lock around chat_completions_create. For now the executor
is single-threaded per pane, so this is unnecessary.
"""
from __future__ import annotations

import os
import select
import sys
import time
import uuid
from dataclasses import dataclass
from typing import IO

from protocol import decode_all, encode


# --- Minimal duck-type of openai.types.chat.ChatCompletion ---
#
# llm.chat() reads only: resp.choices[0].message.content, resp.usage.
# prompt_tokens, resp.usage.completion_tokens. We populate exactly
# those attributes and nothing else — if llm.chat() grows a new
# attribute access, a test will catch it.

@dataclass
class _Message:
    content: str


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _ChatCompletion:
    choices: list[_Choice]
    usage: _Usage


class DelegateClient:
    """Stdio-based LLM client.

    Parameters
    ----------
    stdout : file-like
        Where to write outgoing llm_request frames. Defaults to sys.stdout.
    stdin : file-like
        Where to read incoming llm_response / llm_error frames. Defaults
        to sys.stdin.
    poll_interval : float
        How often to re-read stdin when no matching response has arrived yet.
    timeout : float
        Hard cap on how long to wait for a response before raising.
    """

    def __init__(
        self,
        stdout: IO | None = None,
        stdin: IO | None = None,
        poll_interval: float = 0.2,
        timeout: float = 300.0,
    ):
        self._out = stdout if stdout is not None else sys.stdout
        self._in = stdin if stdin is not None else sys.stdin
        self._poll = poll_interval
        self._timeout = timeout
        # Mirror the openai SDK's .chat.completions.create shape for
        # drop-in use from llm.chat().
        self.chat = _ChatNamespace(self)

    def _new_id(self) -> str:
        return f"req-{uuid.uuid4().hex[:12]}"

    def chat_completions_create(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float | None = None,
        **kwargs,
    ) -> _ChatCompletion:
        rid = self._new_id()
        payload = {
            "id": rid,
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        # encode() reads CLIVE_FRAME_NONCE automatically, so the
        # outgoing frame is authenticated without any explicit wiring
        # at this layer.
        self._out.write(encode("llm_request", payload) + "\n")
        self._out.flush()

        deadline = time.time() + self._timeout
        buf = ""
        while time.time() < deadline:
            chunk = self._read_available()
            if chunk:
                buf += chunk
                # decode_all takes a nonce arg; pass the same one our
                # encode() would have used, so we only accept our own
                # authenticated frames. Reading from env at call time
                # so tests can monkeypatch between calls.
                frames = decode_all(buf, nonce=os.environ.get("CLIVE_FRAME_NONCE", ""))
                for f in frames:
                    if f.kind == "llm_error" and f.payload.get("id") == rid:
                        raise RuntimeError(f.payload.get("error", "delegate error"))
                    if f.kind == "llm_response" and f.payload.get("id") == rid:
                        return _ChatCompletion(
                            choices=[_Choice(message=_Message(
                                content=f.payload.get("content", "")))],
                            usage=_Usage(
                                prompt_tokens=int(f.payload.get("prompt_tokens", 0)),
                                completion_tokens=int(f.payload.get("completion_tokens", 0)),
                            ),
                        )
            # No explicit time.sleep(self._poll) — _read_available uses
            # select() with self._poll as its own timeout, so the loop
            # already paces itself AND stays responsive to the deadline
            # check above.

        raise TimeoutError(
            f"delegate LLM response timed out after {self._timeout}s (id={rid})"
        )

    def _read_available(self) -> str:
        """Read whatever is currently available on the input stream.

        Test fast path: when ``self._in`` is a StringIO (or anything
        with ``getvalue``), read the full remainder in one shot. Tests
        pre-seed buffers before calling, so greedy read is fine and
        avoids the select() path entirely.

        Production path: use ``select.select()`` with a short timeout
        (self._poll) to check readability BEFORE calling readline().
        This is load-bearing for liveness: if we called readline()
        directly on a silent stdin, the call would block until a
        newline arrived, and the caller's deadline check would never
        run — a crashed or hung outer would wedge the inner forever.
        With select() as the sleep mechanism, the caller's while loop
        wakes every poll_interval and checks its deadline.
        """
        if hasattr(self._in, "getvalue"):  # StringIO / BytesIO — test path
            return self._in.read()
        # select() needs a file-like with fileno(). Real sys.stdin has
        # one; if a caller injects an object without, we fall back to
        # readline (and accept the liveness hazard — they opted in).
        try:
            fd_ok = callable(getattr(self._in, "fileno", None))
        except Exception:
            fd_ok = False
        if not fd_ok:
            return self._in.readline()
        try:
            ready, _, _ = select.select([self._in], [], [], self._poll)
        except (OSError, ValueError):
            # stdin closed under us — treat as EOF
            return ""
        if not ready:
            return ""
        return self._in.readline()


class _ChatNamespace:
    """Provides client.chat.completions.create(...) to mimic the openai SDK."""

    def __init__(self, parent: DelegateClient):
        self.completions = _CompletionsNamespace(parent)


class _CompletionsNamespace:
    def __init__(self, parent: DelegateClient):
        self._parent = parent

    def create(self, **kwargs) -> _ChatCompletion:
        return self._parent.chat_completions_create(**kwargs)
