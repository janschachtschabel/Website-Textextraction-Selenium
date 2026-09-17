"""Bounded reusable processes, terminated on timeout or client cancellation.

Browser and conversion pools have separate budgets. Browser sessions themselves
are fresh per job; Python imports and optional NLP models remain warm.
"""

import asyncio
import multiprocessing
import os
import shutil
import signal
import subprocess
import tempfile
from contextlib import suppress

from loguru import logger

from .deadline import Deadline
from .results import CrawlError

# Windows keeps files of just-terminated Chrome processes locked for a moment. Such worker
# directories are removed on a later attempt instead of failing the request that timed out.
_leftover_directories: set[str] = set()


def _remove_leftover_directories():
    for path in list(_leftover_directories):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            _leftover_directories.discard(path)


def _worker(incoming, outgoing, directory):
    if os.name == "posix":
        os.setsid()  # Chrome/ffprobe descendants inherit this process group.
    tempfile.tempdir = directory
    os.environ["TMPDIR"] = directory
    os.environ["TEMP"] = directory
    os.environ["TMP"] = directory
    try:
        while True:
            job = incoming.recv()
            if job is None:
                return
            function, args = job
            try:
                outgoing.send(("ok", function(*args)))
            except CrawlError as exc:
                outgoing.send(("error", str(exc), exc.status_code))
            except Exception as exc:
                outgoing.send(("error", f"Worker task failed ({type(exc).__name__})", 502))
    except (EOFError, BrokenPipeError):
        return  # Parent closed or cancelled this worker.
    finally:
        incoming.close()
        outgoing.close()


class _Slot:
    def __init__(self):
        _remove_leftover_directories()
        self.directory = tempfile.TemporaryDirectory(prefix="extraction-worker-", ignore_cleanup_errors=True)
        context = multiprocessing.get_context("spawn")
        incoming, self.sender = context.Pipe(duplex=False)
        self.receiver, outgoing = context.Pipe(duplex=False)
        self.process = context.Process(target=_worker, args=(incoming, outgoing, self.directory.name), daemon=False)
        self.process.start()
        incoming.close()
        outgoing.close()

    def exchange(self, function, args):
        self.sender.send((function, args))
        return self.receiver.recv()

    def stop(self):
        try:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
            elif self.process.is_alive():
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, timeout=5, check=False
                )
        except (OSError, subprocess.SubprocessError) as exc:
            # Still stop the worker and close its pipes: an open receiver keeps the exchange thread blocked.
            logger.warning("Worker process group kill failed ({})", type(exc).__name__)
        if self.process.is_alive():
            self.process.kill()
        self.process.join(timeout=1)
        self.sender.close()
        self.receiver.close()
        with suppress(ValueError):  # still running despite the kill: nothing more to do here
            self.process.close()
        self.directory.cleanup()
        if os.path.exists(self.directory.name):
            _leftover_directories.add(self.directory.name)


def _stop(slot):
    """Stop a worker; a cleanup failure is logged and never replaces the caller's outcome."""
    try:
        slot.stop()
    except Exception as exc:
        logger.error("Worker cleanup failed ({})", type(exc).__name__)


class WorkerPool:
    def __init__(self, size: int):
        self.idle = asyncio.Queue()
        for _ in range(size):
            self.idle.put_nowait(None)
        self.size = size
        self.slots = set()
        self.running = set()
        self.closed = False

    async def run(self, function, args: tuple, deadline: Deadline):
        if self.closed:
            raise CrawlError("Worker pool is shutting down", 503)
        task = asyncio.current_task()
        self.running.add(task)
        slot = None
        acquired = False
        try:
            slot = await deadline.run(self.idle.get())
            acquired = True
            if slot is None:
                slot = _Slot()
                self.slots.add(slot)
            try:
                reply = await deadline.run(asyncio.to_thread(slot.exchange, function, args))
            except (EOFError, BrokenPipeError, OSError) as exc:
                raise CrawlError("Worker exited unexpectedly", 503) from exc
            if reply[0] == "error":
                raise CrawlError(reply[1], reply[2])
            return reply[1]
        except BaseException:
            if slot is not None:
                # Never hand a stopped worker back to the idle queue, even if cleanup fails.
                dead, slot = slot, None
                self.slots.discard(dead)
                _stop(dead)
            raise
        finally:
            if acquired and not self.closed:
                self.idle.put_nowait(slot)
            self.running.discard(task)

    async def close(self):
        self.closed = True
        tasks = list(self.running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for slot in self.slots:
            _stop(slot)
        self.slots.clear()
        _remove_leftover_directories()

    def stats(self):
        return {
            "limit": self.size,
            "started": len(self.slots),
            "busy": self.size - self.idle.qsize(),
            "closed": self.closed,
        }
