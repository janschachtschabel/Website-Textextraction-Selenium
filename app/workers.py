"""Bounded reusable processes, terminated on timeout or client cancellation and
retired after a fixed number of jobs.

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
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from loguru import logger

from .deadline import Deadline
from .results import CrawlError

# Windows keeps files of just-terminated Chrome processes locked for a moment. Such worker
# directories are removed on a later attempt instead of failing the request that timed out.
_leftover_directories: set[str] = set()
GRACE_SECONDS = 2  # an idle worker leaves in milliseconds


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
        self.jobs = 0
        self.exitcode = None

    def exchange(self, function, args):
        self.sender.send((function, args))
        return self.receiver.recv()

    def stop(self, graceful: bool = False):
        """Stop the worker and its children.

        ``graceful`` asks an idle worker to leave on its own first, so it can flush what it
        holds - buffered output, coverage data. One that does not leave in time is killed.
        """
        if not (graceful and self._leaves_on_request()):
            self._kill()
        self.process.join(timeout=1)
        self.exitcode = self.process.exitcode
        self.sender.close()
        self.receiver.close()
        with suppress(ValueError):  # still running despite the kill: nothing more to do here
            self.process.close()
        self.directory.cleanup()
        if os.path.exists(self.directory.name):
            _leftover_directories.add(self.directory.name)

    def _leaves_on_request(self) -> bool:
        with suppress(OSError, ValueError):
            self.sender.send(None)
            self.process.join(timeout=GRACE_SECONDS)
        return not self.process.is_alive()

    def _kill(self):
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


def _stop(slot, graceful: bool = False):
    """Stop a worker; a cleanup failure is logged and never replaces the caller's outcome."""
    try:
        slot.stop(graceful)
    except Exception as exc:
        logger.error("Worker cleanup failed ({})", type(exc).__name__)


class WorkerPool:
    def __init__(self, size: int, max_jobs: int = 100):
        self.idle = asyncio.Queue()
        for _ in range(size):
            self.idle.put_nowait(None)
        self.size = size
        self.max_jobs = max_jobs
        self.slots = set()
        self.running = set()
        self.pending = set()  # worker starts and stops running on those threads
        # Two threads per slot: a job holds one until its worker answers, and stopping a worker
        # that never answered needs a second. Own threads, because the loop's shared executor
        # also resolves every proxied browser connection.
        self.executor = ThreadPoolExecutor(max_workers=2 * size, thread_name_prefix="extraction-worker")
        self.closed = False

    async def _completed(self, future):
        """Wait for pool work without cancelling it: asyncio.wrap_future chains a cancellation
        back to the thread pool, which would drop a queued stop and leave its worker running."""
        return await asyncio.shield(asyncio.wrap_future(future))

    async def _in_pool(self, function, *args):
        # A coroutine, so an expired deadline closes it before a worker is handed the job.
        return await asyncio.wrap_future(self.executor.submit(function, *args))

    def _track(self, future):
        """Background work close() must not leave behind."""
        self.pending.add(future)
        future.add_done_callback(self.pending.discard)
        return future

    def _retire(self, slot, graceful: bool = False):
        """Stop a worker off the event loop: taskkill, join and rmtree take seconds."""
        self.slots.discard(slot)
        return self._track(self.executor.submit(_stop, slot, graceful))

    async def _started(self):
        """Start a worker off the loop; a cancellation must not leave the process running."""
        starting = self._track(self.executor.submit(_Slot))
        try:
            return await asyncio.wrap_future(starting)
        except BaseException:
            starting.add_done_callback(self._discard_started)
            raise

    def _discard_started(self, starting):
        if not starting.cancelled() and starting.exception() is None:
            self._retire(starting.result(), True)

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
                slot = await self._started()
                self.slots.add(slot)
            try:
                reply = await deadline.run(self._in_pool(slot.exchange, function, args))
            except (EOFError, BrokenPipeError, OSError) as exc:
                raise CrawlError("Worker exited unexpectedly", 503) from exc
            if reply[0] == "error":
                raise CrawlError(reply[1], reply[2])
            slot.jobs += 1
            if slot.jobs >= self.max_jobs:
                # lxml, MarkItDown and Chrome keep memory that only a new process gives back.
                retired, slot = slot, None  # the idle queue gets None: the next job starts a worker
                await self._completed(self._retire(retired, True))  # idle: let it flush and exit
            return reply[1]
        except BaseException:
            if slot is not None:
                # Never hand a stopped worker back to the idle queue, even if cleanup fails.
                dead, slot = slot, None
                with suppress(asyncio.CancelledError):
                    # A task being cancelled cannot wait for its own cleanup; close() does.
                    await self._completed(self._retire(dead))
            raise
        finally:
            if acquired and not self.closed:
                self.idle.put_nowait(slot)
            self.running.discard(task)

    async def close(self):
        if self.closed:
            return  # closing twice must not submit to an executor that is already gone
        self.closed = True
        tasks = list(self.running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Whatever was busy has been cancelled and stopped above; these slots are idle.
        for slot in list(self.slots):
            self._retire(slot, True)
        # A worker still starting adds its own stop once it is there, so wait until nothing is left.
        while self.pending:
            waiting = [self._completed(future) for future in list(self.pending)]
            await asyncio.gather(*waiting, return_exceptions=True)
        await self._in_pool(_remove_leftover_directories)
        self.executor.shutdown(wait=False)

    def stats(self):
        return {
            "limit": self.size,
            "started": len(self.slots),
            "busy": self.size - self.idle.qsize(),
            "closed": self.closed,
        }
