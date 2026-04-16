"""Per-pane asyncio loop on a background thread.

Bridges the synchronous interactive_runner to the async observation
and speculation pipelines. Each pane gets its own loop so panes can't
starve each other.

Typical usage:
    loop = PaneLoop.start()
    fut = loop.submit(some_coroutine())
    result = fut.result(timeout=...)
    loop.stop()  # signals the loop thread to shut down
"""
import asyncio
import threading
from concurrent.futures import Future


class PaneLoop:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stopped = False
        self.thread: threading.Thread | None = None

    @classmethod
    def start(cls) -> "PaneLoop":
        self = cls()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="clive-pane-loop",
        )
        self.thread.start()
        # Block briefly until the loop is live and ready to accept work.
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("PaneLoop failed to start within 2s")
        return self

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            # Cancel any pending tasks and close cleanly.
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    def submit(self, coro) -> Future:
        if self._stopped or self._loop is None:
            raise RuntimeError("PaneLoop is stopped")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 2.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self.thread:
            self.thread.join(timeout=timeout)
