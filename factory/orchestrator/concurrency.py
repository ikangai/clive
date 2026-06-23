"""Worker concurrency cap (spec §5, §9, §11). Evaluation is embarrassingly
parallel but capped — neither the loop nor the workers may run faster than budget
allows. Gain is low by construction."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable


def run_capped(tasks: Iterable[Callable[[], dict]], cap: int,
               on_done: Callable[[dict], bool] | None = None) -> list[dict]:
    """Run thunks with at most `cap` in flight. `on_done(result)` is called as
    each finishes; if it returns False, no further tasks are submitted (circuit
    breaker / budget stop). Already-running tasks are allowed to finish."""
    tasks = list(tasks)
    results: list[dict] = []
    stop = False
    with ThreadPoolExecutor(max_workers=max(1, cap)) as ex:
        futures = {}
        it = iter(tasks)
        # prime the pool
        for _ in range(min(cap, len(tasks))):
            try:
                futures[ex.submit(next(it))] = True
            except StopIteration:
                break
        while futures:
            for fut in as_completed(list(futures)):
                del futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # a worker crash must not sink the round
                    res = {"outcome": "error", "error": str(e)}
                results.append(res)
                if on_done is not None and on_done(res) is False:
                    stop = True
                if not stop:
                    try:
                        futures[ex.submit(next(it))] = True
                    except StopIteration:
                        pass
                break  # re-evaluate as_completed over the shrunken set
    return results
