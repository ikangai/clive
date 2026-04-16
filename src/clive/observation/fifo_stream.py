"""Per-pane FIFO byte stream with async reader + L2 classifier + fan-out.

Lifecycle:
  from_fifo_path(path) -> start reader coroutine on current event loop
  subscribe()          -> register an asyncio.Queue; get ByteEvents
  close()              -> cancel reader, close fd

The caller is responsible for the FIFO's existence and for running
`tmux pipe-pane -o 'cat > <fifo_path>'` -- PaneStream is the reader side
only. (See Task 1.4 for the pane-lifecycle wiring.)

The reader opens the FIFO O_NONBLOCK so close() doesn't deadlock
waiting for a writer. Each chunk read feeds the ByteClassifier;
resulting events fan out to all subscribers.
"""
import asyncio
import logging
import os
import time

from byte_classifier import ByteClassifier, ByteEvent  # noqa: F401

log = logging.getLogger(__name__)

_CHUNK_SIZE = 4096
_SUBSCRIBER_QUEUE_SIZE = 256


class PaneStream:
    def __init__(self, fifo_path: str):
        self.fifo_path = fifo_path
        self.classifier = ByteClassifier()
        self.last_byte_ts = time.monotonic()
        self.subscribers: list[asyncio.Queue] = []
        self._closed = False
        self._reader_task: asyncio.Task | None = None

    @classmethod
    def from_fifo_path(cls, fifo_path: str) -> "PaneStream":
        assert os.path.exists(fifo_path), fifo_path
        self = cls(fifo_path)
        self._reader_task = asyncio.create_task(self._read_loop())
        return self

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self.subscribers.append(q)
        return q

    async def _read_loop(self):
        fd = os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            while not self._closed:
                try:
                    chunk = os.read(fd, _CHUNK_SIZE)
                except BlockingIOError:
                    await asyncio.sleep(0.005)
                    continue
                if not chunk:
                    # FIFO EOF (writer closed). Keep re-reading; a new
                    # writer may attach. Sleep to avoid busy loop.
                    await asyncio.sleep(0.02)
                    continue
                self.last_byte_ts = time.monotonic()
                events = self.classifier.feed(chunk)
                for ev in events:
                    for q in self.subscribers:
                        try:
                            q.put_nowait(ev)
                        except asyncio.QueueFull:
                            log.warning(
                                "subscriber queue full, dropping %s event",
                                ev.kind,
                            )
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
