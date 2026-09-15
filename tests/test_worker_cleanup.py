import tempfile
import time
from pathlib import Path

import pytest

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
