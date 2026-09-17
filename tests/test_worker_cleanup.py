import shutil
import tempfile
import time
from pathlib import Path

import pytest

from app import workers
from app.deadline import Deadline
from app.results import CrawlError
from app.workers import WorkerPool


def temp_directory():
    return tempfile.gettempdir()


def stuck_converter():
    Path(tempfile.gettempdir(), "partial-output.txt").write_text("incomplete")
    time.sleep(30)


async def test_timeout_removes_partial_files_and_worker_profile():
    pool = WorkerPool(1)
    try:
        directory = await pool.run(temp_directory, (), Deadline(5))
        with pytest.raises(CrawlError):
            await pool.run(stuck_converter, (), Deadline(0.2))
        assert not Path(directory).exists()
    finally:
        await pool.close()


async def test_locked_worker_directory_does_not_break_timeout_or_pool():
    pool = WorkerPool(1)
    try:
        directory = Path(await pool.run(temp_directory, (), Deadline(5)))
        # On Windows a just-terminated Chrome can still hold files open like this.
        handle = (directory / "locked.log").open("w")
        try:
            with pytest.raises(CrawlError, match="deadline"):
                await pool.run(stuck_converter, (), Deadline(0.2))
        finally:
            handle.close()
        assert await pool.run(temp_directory, (), Deadline(5)) != str(directory)
        assert not directory.exists()
    finally:
        await pool.close()


async def test_worker_directory_that_cannot_be_removed_yet_is_removed_later(monkeypatch):
    real_rmtree = shutil.rmtree
    locked = set()

    def rmtree(path, *args, **kwargs):
        if str(path) not in locked:  # a locked directory stays, as on Windows
            real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    pool = WorkerPool(1)
    try:
        directory = await pool.run(temp_directory, (), Deadline(5))
        locked.add(directory)
        with pytest.raises(CrawlError, match="deadline"):
            await pool.run(stuck_converter, (), Deadline(0.2))
        assert Path(directory).exists()
        locked.clear()
        assert await pool.run(temp_directory, (), Deadline(5)) != directory
        assert not Path(directory).exists()
    finally:
        await pool.close()


async def test_failed_worker_cleanup_keeps_original_error_and_is_not_reused(monkeypatch):
    real_stop = workers._Slot.stop

    def failing_stop(slot):
        real_stop(slot)
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(workers._Slot, "stop", failing_stop)
    pool = WorkerPool(1)
    try:
        with pytest.raises(CrawlError, match="deadline"):
            await pool.run(stuck_converter, (), Deadline(0.2))
        monkeypatch.undo()
        assert await pool.run(temp_directory, (), Deadline(5))
    finally:
        await pool.close()
