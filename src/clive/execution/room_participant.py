"""Transport-agnostic client-side room participant.

Owns the per-session nonce and the member's identity; turns inbound
framed lines from the lobby into outbound framed lines (via
``room_runner.decide_turn``) without touching sockets, pipes, or
threads. The glue layer (e.g. ``ConvLoop`` + a socket or a tmux pane
reader) is what routes bytes between this object and the wire.

Usage shape:

    p = RoomParticipant(name="alice", nonce=os.environ["CLIVE_FRAME_NONCE"],
                        llm_client=get_llm_client())
    # On connection open:
    for frame_str in p.bootstrap(rooms=["general"]):
        transport.write((frame_str + "\\n").encode())

    # For each line read from the lobby:
    for frame_str in p.on_line(line):
        transport.write((frame_str + "\\n").encode())

This separation mirrors the lobby side: ``lobby_state`` is the pure
decider, ``lobby_server`` is the IO wrapper. Tests can exercise the
full decision tree without sockets, and the IO composition is a
handful of trivial lines.

See docs/plans/2026-04-14-clive-rooms-design.md §6.2 (room runner)
and §6.3 (membership declaration — the three converging bootstraps
all resolve to the same pair of frames this object emits).
"""
from __future__ import annotations

from typing import Optional

from protocol import Frame, decode_all, encode
from room_runner import decide_turn


class RoomParticipant:
    """Stateful per-session participant. Thread-compatible but not
    thread-safe — the caller drives ``on_line`` sequentially from a
    single reader."""

    def __init__(
        self,
        name: str,
        nonce: str,
        llm_client,
        *,
        driver_text: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.name = name
        self.nonce = nonce
        self.llm_client = llm_client
        self._driver_text = driver_text
        self._model = model

    # ─── Bootstrap ──────────────────────────────────────────────────

    def bootstrap(self, rooms: list[str]) -> list[str]:
        """Frames to emit on connection open: session_hello followed
        by one join_room per requested room. All three of design
        §6.3's declaration paths (CLI flag, config file, runtime
        task) collapse to this same sequence."""
        out = [
            self._encode("session_hello",
                         {"client_kind": "clive", "name": self.name}),
        ]
        for room in rooms:
            out.append(self._encode("join_room", {"room": room}))
        return out

    # ─── Inbound dispatch ───────────────────────────────────────────

    def on_line(self, line: str) -> list[str]:
        """Process one line of lobby traffic. Frames whose nonce
        does not match ``self.nonce`` are silently dropped (§7.2) so
        a compromised lobby or a prompt-injection echo cannot forge
        a ``your_turn``."""
        out: list[str] = []
        for frame in decode_all(line, nonce=self.nonce):
            out.extend(self._dispatch(frame))
        return out

    # ─── Internals ──────────────────────────────────────────────────

    def _dispatch(self, frame: Frame) -> list[str]:
        if frame.kind == "your_turn":
            kind, payload = decide_turn(
                frame.payload,
                llm_client=self.llm_client,
                driver_text=self._driver_text,
                model=self._model,
            )
            return [self._encode(kind, payload)]
        # All other kinds (session_ack, thread_opened, say/pass
        # fanout, nack, threads) are informational — a richer client
        # would log or surface them; the participant itself emits
        # nothing back. Silence preserves turn discipline: only a
        # `your_turn` causes an outbound frame.
        return []

    def _encode(self, kind: str, payload: dict) -> str:
        return encode(kind, payload, nonce=self.nonce)
