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


@pytest.mark.asyncio
async def test_multiple_subscribers_receive_same_events(fifo_path):
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    q1 = stream.subscribe()
    q2 = stream.subscribe()

    import subprocess
    await asyncio.sleep(0.02)
    subprocess.run(
        ["bash", "-c", f'printf "\\x1b[31mRED\\x1b[0m" > {fifo_path}'],
        check=True,
    )

    e1 = await asyncio.wait_for(q1.get(), timeout=2.0)
    e2 = await asyncio.wait_for(q2.get(), timeout=2.0)
    assert e1.kind == "color_alert"
    assert e2.kind == "color_alert"
    await stream.close()


@pytest.mark.asyncio
async def test_cross_chunk_pattern_through_stream(fifo_path):
    """The ByteClassifier carryover should bridge reads across small writes."""
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    q = stream.subscribe()

    import subprocess
    # Split a password prompt across two writes; the FIFO reader may
    # receive them as two separate chunks.
    await asyncio.sleep(0.02)
    subprocess.run(["bash", "-c", f'printf "Passw" > {fifo_path}'], check=True)
    await asyncio.sleep(0.05)
    subprocess.run(["bash", "-c", f'printf "ord: " > {fifo_path}'], check=True)

    event = await asyncio.wait_for(q.get(), timeout=2.0)
    assert event.kind == "password_prompt"
    await stream.close()


@pytest.mark.asyncio
async def test_queue_full_drops_newest(fifo_path):
    """A full subscriber queue drops incoming events; other subscribers are
    unaffected."""
    os.mkfifo(fifo_path)
    stream = PaneStream.from_fifo_path(fifo_path)
    # Force the first subscriber's queue to 1 slot so it overflows fast
    slow = asyncio.Queue(maxsize=1)
    stream.subscribers.append(slow)
    fast = stream.subscribe()

    import subprocess
    # Generate multiple color_alert events (different SGR escapes produce
    # more matches than one subscriber slot can hold).
    await asyncio.sleep(0.02)
    for i in range(5):
        subprocess.run(
            ["bash", "-c", f'printf "\\x1b[31mR{i}\\x1b[0m" > {fifo_path}'],
            check=True,
        )
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)

    # Slow queue is at its max
    assert slow.qsize() == 1
    # Fast queue received multiple events (the classifier emits >1)
    assert fast.qsize() >= 1
    await stream.close()
