import asyncio
import threading

import anyio
import pytest

from assistant_agent.async_workers import AsyncWorker


async def test_worker_keeps_capacity_until_cancelled_call_finishes():
    worker = AsyncWorker(capacity=1)
    started = asyncio.Event()
    second_started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    scope = anyio.CancelScope()

    def first():
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("Worker was not released")

    async def run_first():
        with scope:
            await worker.run(first)

    first_task = asyncio.create_task(run_first())
    second_task = None
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        scope.cancel()
        second_task = asyncio.create_task(
            worker.run(lambda: loop.call_soon_threadsafe(second_started.set))
        )
        for _ in range(3):
            await asyncio.sleep(0)
        assert worker.limiter.borrowed_tokens == 1
        assert not first_task.done()
        assert not second_started.is_set()
    finally:
        release.set()
        await asyncio.wait_for(first_task, timeout=2)
        if second_task is not None:
            await asyncio.wait_for(second_task, timeout=2)
    assert second_started.is_set()
    assert worker.limiter.borrowed_tokens == 0


async def test_worker_releases_capacity_after_failure():
    worker = AsyncWorker(capacity=1)

    def fail():
        raise ValueError("SDK failure")

    with pytest.raises(ValueError, match="SDK failure"):
        await worker.run(fail)
    assert await worker.run(lambda: "next call") == "next call"
    assert worker.limiter.borrowed_tokens == 0
