import asyncio
import os
import time

import pytest

from app.capacity import Capacity
from app.deadline import Deadline
from app.results import CrawlError
from app.workers import WorkerPool


def identity(value):
    return os.getpid(), value


def hang():
    time.sleep(60)


async def test_workers_are_lazy_reused_and_hung_process_is_replaced():
    pool = WorkerPool(1)
    assert pool.stats()["started"] == 0
    try:
        first, value = await pool.run(identity, ("one",), Deadline(5))
        second, _ = await pool.run(identity, ("two",), Deadline(5))
        assert first == second and value == "one"
        started = time.monotonic()
        with pytest.raises(CrawlError, match="deadline"):
            await pool.run(hang, (), Deadline(0.2))
        assert time.monotonic() - started < 5  # the process is killed, not awaited
        third, value = await pool.run(identity, ("recovered",), Deadline(5))
        assert third != first and value == "recovered"
        if os.name == "posix":
            with pytest.raises(ProcessLookupError):
                os.kill(first, 0)
    finally:
        await pool.close()
    assert pool.stats()["started"] == 0


async def test_cancelled_worker_is_stopped_and_slot_released():
    pool = WorkerPool(1)
    try:
        await pool.run(identity, (None,), Deadline(5))
        task = asyncio.create_task(pool.run(hang, (), Deadline(30)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await pool.run(identity, ("ready",), Deadline(5)))[1] == "ready"
    finally:
        await pool.close()


async def test_capacity_rejects_excess_queue_and_does_not_release_unowned_slot():
    capacity = Capacity(1, 1, 10)
    async with capacity.slot(Deadline(5)):

        async def waiting():
            async with capacity.slot(Deadline(0.15)):
                pytest.fail("No slot was available")

        task = asyncio.create_task(waiting())
        await asyncio.sleep(0.01)
        with pytest.raises(CrawlError, match="queue"):
            async with capacity.slot(Deadline(5)):
                pytest.fail("Queue should be full")
        with pytest.raises(CrawlError):
            await task
        assert capacity.active == 1 and capacity.waiting == 0
    assert capacity.active == 0
