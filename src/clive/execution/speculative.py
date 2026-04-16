"""Speculative LLM call scheduler.

Fires chat() calls on high-confidence L2 triggers so the main model's
inference overlaps with pane settling. Version-stamped; newer results
supersede older. Bounded concurrency + rate limit + circuit breaker
bound the cost.

Designed to run on a PaneLoop (Task 1.5). For unit tests, pass a
MagicMock pane_loop whose .submit() returns pre-configured Futures.
"""
import collections
import logging
import time
from dataclasses import dataclass
from typing import Any

from byte_classifier import ByteEvent

log = logging.getLogger(__name__)

SPEC_TRIGGERS: frozenset[str] = frozenset({
    "cmd_end",
    "password_prompt",
    "confirm_prompt",
    "error_keyword",
    "permission_error",
})


@dataclass
class SpecCall:
    version: int
    trigger: ByteEvent
    future: Any                          # concurrent.futures.Future (or mock)
    messages_snapshot: list[dict]
    started_at: float


class SpeculationScheduler:
    """Version-stamped speculative LLM scheduler.

    Call flow:
      fire(trigger, messages_snapshot) — submits a chat() coroutine on
        the pane_loop; returns True iff accepted (not rate-limited or
        breaker-tripped).
      try_consume(current_messages) — returns (reply, pt, ct) of the
        newest completed call whose snapshot matches the current
        message-prefix; (None, 0, 0) otherwise.

    Guarantees:
      - At most MAX_IN_FLIGHT concurrent calls per scheduler.
      - At most one fire per MIN_FIRE_INTERVAL seconds.
      - A result is only accepted if its version > accepted_version AND
        its messages_snapshot is a prefix of current_messages.
      - On accept, older in-flight calls are cancelled.
    """
    MAX_IN_FLIGHT = 2
    MIN_FIRE_INTERVAL = 0.2          # seconds
    BREAKER_THRESHOLD = 5            # cancellations/window
    BREAKER_WINDOW = 60.0            # seconds

    def __init__(self, client, model: str, pane_loop=None):
        self.client = client
        self.model = model
        self.pane_loop = pane_loop
        self.in_flight: list[SpecCall] = []
        self.latest_version: int = 0
        self.accepted_version: int = 0
        self._last_fire_ts: float = 0.0
        self._cancel_times: collections.deque = collections.deque(maxlen=32)

    # --- Public API ----------------------------------------------------

    def fire(self, trigger: ByteEvent, messages_snapshot: list[dict]) -> bool:
        """Submit a speculative chat call. Returns True if fired, False if
        rate-limited or breaker-tripped."""
        if self._breaker_tripped():
            return False

        now = time.monotonic()
        if now - self._last_fire_ts < self.MIN_FIRE_INTERVAL:
            return False
        self._last_fire_ts = now

        # Maintain MAX_IN_FLIGHT by cancelling the oldest. We cancel
        # oldest (not newest) because the newest call is most likely to
        # reflect current state.
        while len(self.in_flight) >= self.MAX_IN_FLIGHT:
            oldest = min(self.in_flight, key=lambda c: c.version)
            oldest.future.cancel()
            self.in_flight.remove(oldest)
            self._record_cancel()

        self.latest_version += 1
        v = self.latest_version
        future = self._submit_call(v, list(messages_snapshot))
        self.in_flight.append(SpecCall(
            version=v,
            trigger=trigger,
            future=future,
            messages_snapshot=list(messages_snapshot),
            started_at=now,
        ))
        return True

    def try_consume(self, current_messages: list[dict]) -> tuple[str | None, int, int]:
        """Return (reply, prompt_tokens, completion_tokens) of the newest
        completed call whose snapshot matches current_messages. Returns
        (None, 0, 0) when no such call is ready.

        Side effects on success: advances accepted_version, cancels older
        in-flight calls.
        """
        # Newest first
        for call in sorted(self.in_flight, key=lambda c: -c.version):
            if call.version <= self.accepted_version:
                continue
            if not call.future.done():
                continue
            if not self._snapshot_matches(call.messages_snapshot, current_messages):
                self.in_flight.remove(call)
                continue
            try:
                reply, pt, ct = call.future.result()
            except Exception as exc:
                log.warning("speculative call v=%d raised: %r", call.version, exc)
                self.in_flight.remove(call)
                continue
            self.accepted_version = call.version
            self._cancel_older_than(call.version)
            return reply, pt, ct
        return None, 0, 0

    # --- Internal ------------------------------------------------------

    def _snapshot_matches(self, snap: list[dict], current: list[dict]) -> bool:
        """The snapshot taken at fire time must still be a prefix of
        current_messages for the result to be relevant."""
        if len(snap) > len(current):
            return False
        return current[: len(snap)] == snap

    def _cancel_older_than(self, version: int) -> None:
        remaining: list[SpecCall] = []
        for call in self.in_flight:
            if call.version <= version:
                if not call.future.done():
                    call.future.cancel()
                    self._record_cancel()
            else:
                remaining.append(call)
        self.in_flight = remaining

    def _record_cancel(self) -> None:
        self._cancel_times.append(time.monotonic())

    def _breaker_tripped(self) -> bool:
        now = time.monotonic()
        recent = sum(1 for t in self._cancel_times if now - t <= self.BREAKER_WINDOW)
        return recent > self.BREAKER_THRESHOLD

    def _submit_call(self, version: int, messages_snapshot: list[dict]):
        """Submit the chat call coroutine on the pane loop.

        For production (Task 2.2 integration), pane_loop is a real
        PaneLoop. For unit tests, pane_loop.submit is mocked to return
        a prepared Future.
        """
        if self.pane_loop is None:
            # No loop provided: caller is doing a no-op smoke test.
            # Return a Future-ish that's immediately done with an empty
            # reply. This path is NOT used by Task 2.2; it exists so that
            # SpeculationScheduler is constructable without a loop.
            from concurrent.futures import Future
            f = Future()
            f.set_result(("", 0, 0))
            return f
        return self.pane_loop.submit(_run_call(self.client, self.model, messages_snapshot))


async def _run_call(client, model: str, messages: list[dict]):
    """Thin coroutine wrapper around chat_stream for speculative execution."""
    from llm import chat_stream
    # chat_stream is sync; run it in the default executor so it doesn't
    # block the pane loop. The coroutine yields (reply, pt, ct).
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: chat_stream(client, messages, model=model),
    )
