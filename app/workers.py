"""Bounded reusable processes, terminated on timeout or client cancellation.

Browser and conversion pools have separate budgets. Browser sessions themselves
are fresh per job; Python imports and optional NLP models remain warm.
"""

import asyncio
import multiprocessing
import os
import signal
import subprocess
import tempfile
from contextlib import suppress

from .deadline import Deadline
from .results import CrawlError


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
        self.directory = tempfile.TemporaryDirectory(prefix="extraction-worker-")
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
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        elif self.process.is_alive():
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, timeout=5, check=False
            )
        if self.process.is_alive():
            self.process.kill()
        self.process.join(timeout=1)
        self.sender.close()
        self.receiver.close()
        self.process.close()
        self.directory.cleanup()


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
                self.slots.discard(slot)
                slot.stop()
                slot = None
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
            slot.stop()
        self.slots.clear()

    def stats(self):
        return {
            "limit": self.size,
            "started": len(self.slots),
            "busy": self.size - self.idle.qsize(),
            "closed": self.closed,
        }
