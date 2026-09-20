import asyncio
import os
import threading
import time

import pytest

from app import workers
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


async def test_worker_is_retired_after_its_job_budget():
    pool = WorkerPool(1, max_jobs=2)
    try:
        first, _ = await pool.run(identity, ("one",), Deadline(5))
        second, _ = await pool.run(identity, ("two",), Deadline(5))
        assert pool.stats()["started"] == 0  # retired right after its second job
        third, value = await pool.run(identity, ("three",), Deadline(5))
        assert first == second != third and value == "three"
        if os.name == "posix":
            with pytest.raises(ProcessLookupError):
                os.kill(first, 0)
    finally:
        await pool.close()


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


async def test_worker_startup_and_teardown_keep_off_the_event_loop(monkeypatch):
    """taskkill, join and rmtree take seconds; the loop must keep serving other requests."""
    loop_thread = threading.get_ident()
    threads = []
    real_stop, real_slot = workers._stop, workers._Slot

    class RecordingSlot(real_slot):
        def __init__(self):
            threads.append(threading.get_ident())
            super().__init__()

    def recording_stop(slot, graceful=False):
        threads.append(threading.get_ident())
        real_stop(slot, graceful)

    monkeypatch.setattr(workers, "_Slot", RecordingSlot)
    monkeypatch.setattr(workers, "_stop", recording_stop)
    pool = WorkerPool(1)
    try:
        await pool.run(identity, ("one",), Deadline(5))
        with pytest.raises(CrawlError, match="deadline"):
            await pool.run(hang, (), Deadline(0.2))
    finally:
        await pool.close()
    assert len(threads) >= 2 and loop_thread not in threads


async def test_a_cancellation_while_a_worker_starts_does_not_leak_it(monkeypatch):
    started = []

    class SlowSlot(workers._Slot):
        def __init__(self):
            time.sleep(0.4)  # long enough for the cancellation to arrive mid-startup
            super().__init__()
            started.append(self)

    monkeypatch.setattr(workers, "_Slot", SlowSlot)
    pool = WorkerPool(1)
    try:
        task = asyncio.create_task(pool.run(identity, ("one",), Deadline(10)))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await pool.close()
    assert len(started) == 1
    assert started[0].exitcode is not None  # stopped, not left running


async def test_a_cleanup_that_is_still_queued_survives_a_second_cancellation(monkeypatch):
    """Cancelling the task again must not cancel the stop it just handed to the pool."""
    created, stopped, release = [], [], threading.Event()
    real_slot, real_stop = workers._Slot, workers._stop

    class TrackingSlot(real_slot):
        def __init__(self):
            super().__init__()
            created.append(self)

    def recording_stop(slot, graceful=False):
        stopped.append(slot)
        real_stop(slot, graceful)

    monkeypatch.setattr(workers, "_Slot", TrackingSlot)
    monkeypatch.setattr(workers, "_stop", recording_stop)
    pool = WorkerPool(1)
    try:
        await pool.run(identity, ("warm",), Deadline(5))
        pool.executor.submit(release.wait)  # the job takes one thread, this the other
        task = asyncio.create_task(pool.run(hang, (), Deadline(30)))
        await asyncio.sleep(0.1)
        task.cancel()
        while not pool.pending:  # wait for the stop to be queued behind the busy threads
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await pool.close()
        assert stopped == created
    finally:
        release.set()
        for slot in created:
            if slot.exitcode is None:
                real_stop(slot)


async def test_closing_twice_is_a_no_op():
    pool = WorkerPool(1)
    await pool.run(identity, ("one",), Deadline(5))
    await pool.close()
    await pool.close()  # the executor is gone; a second close must not submit to it
    assert pool.stats()["started"] == 0
