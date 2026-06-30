"""Concurrency test for discovery.tool_memo — thread-safe record (gh#41).

auto_explore spawns one daemon thread per unknown tool (discovery/auto.py),
so a subtask with several tools runs several explorations that each call
``record_tool_memo`` near-simultaneously. The persisted ``tool_memos.json``
must not lose entries to a load-modify-write race or a shared-tmp clobber:
every tool that recorded a memo must still be present after the threads join.

Pure-unit: threads + a tmp CLIVE_HOME only. No tmux, no network.
"""
import json
import threading

import pytest

from discovery import tool_memo


@pytest.fixture(autouse=True)
def _redirect_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIVE_HOME", str(tmp_path))
    return tmp_path


def test_concurrent_records_do_not_clobber_each_other(_redirect_home):
    n = 24
    barrier = threading.Barrier(n)
    names = [f"tool_{i:02d}" for i in range(n)]

    def worker(name: str) -> None:
        # Align every thread at the write so the load-modify-replace overlaps.
        barrier.wait()
        tool_memo.record_tool_memo(name, f"{name} --run", f"usage for {name}")

    threads = [threading.Thread(target=worker, args=(name,)) for name in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every tool's learned invocation must survive in the on-disk cache.
    data = json.loads((_redirect_home / "tool_memos.json").read_text())
    assert set(data) == set(names)
    for name in names:
        memo = tool_memo.load_tool_memo(name)
        assert memo is not None, f"{name} memo was lost to a write race"
        assert memo["invocation"] == f"{name} --run"
        assert memo["usage"] == f"usage for {name}"
