"""Integration tests for PaneStream — uses real mkfifo + subprocess writer."""
import asyncio
import os
import pytest
import tempfile

from fifo_stream import PaneStream


@pytest.fixture
def fifo_path():
    d = tempfile.mkdtemp(prefix="clive-fifo-test-")
    p = os.path.join(d, "test.fifo")
    yield p
    if os.path.exists(p):
        os.unlink(p)
    os.rmdir(d)


@pytest.mark.asyncio
async def test_read_loop_emits_events_to_subscriber(fifo_path):
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    q = stream.subscribe()

    async def write():
        await asyncio.sleep(0.05)
        import subprocess
        subprocess.run(
            ["bash", "-c", f'printf "\\x1b[31mERROR\\x1b[0m" > {fifo_path}'],
            check=True,
        )

    writer = asyncio.create_task(write())
    event = await asyncio.wait_for(q.get(), timeout=2.0)
    assert event.kind == "color_alert"
    await writer
    await stream.close()


@pytest.mark.asyncio
async def test_activity_heartbeat_updates(fifo_path):
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    t_before = stream.last_byte_ts

    import subprocess
    await asyncio.sleep(0.01)
    subprocess.run(["bash", "-c", f'echo hi > {fifo_path}'], check=True)
    await asyncio.sleep(0.1)

    assert stream.last_byte_ts > t_before
    await stream.close()


@pytest.mark.asyncio
async def test_close_cancels_reader(fifo_path):
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    await stream.close()
    assert stream._reader_task.done()
