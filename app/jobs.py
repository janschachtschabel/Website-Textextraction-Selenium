"""Background batch jobs for clients whose connection cannot stay open that long.

A Cloudflare quick tunnel ends a request after about 125 seconds, while a batch may run
for as long as its deadline allows. Job records live in the shared state store, so every
Uvicorn worker can answer a poll; the work itself runs in the process that accepted it.
"""

import asyncio
import secrets
import time
from datetime import UTC, datetime

from loguru import logger

from .results import CrawlError

KEY = "job:"
LOST_AFTER_SECONDS = 30  # an unfinished job this far past its deadline has lost its process
# The record's progress is saved at most this often. Each URL already costs one row write;
# without this it would cost a record write as well, in a store shared across processes.
# _run saves once more at the end, so the final count is never the one the throttle swallowed.
PROGRESS_EVERY_SECONDS = 2.0
UNFINISHED = {"queued", "running"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_key(job_id: str, seq: int) -> str:
    """Rows are numbered from 0 in the order their URLs finished."""
    return f"{KEY}{job_id}:row:{seq}"


class JobRunner:
    def __init__(self, resources):
        self.resources = resources
        self.config = resources.config
        self.tasks: dict[str, asyncio.Task | None] = {}

    async def submit(self, urls, options, max_concurrency) -> str:
        if len(self.tasks) >= self.config.max_active_jobs:
            raise CrawlError("Too many active jobs", 503)
        job_id = secrets.token_urlsafe(16)
        self.tasks[job_id] = None  # reserved before the first await, so the bound holds
        record = {
            "status": "queued",
            "progress": {"done": 0, "succeeded": 0, "total": len(urls)},
            "submitted_at": _now(),
            "deadline_at": time.time() + options.timeout_ms / 1000,
            "finished_at": None,
            "error": None,
        }
        try:
            await self._save(job_id, record)
        except BaseException:
            self.tasks.pop(job_id, None)
            raise
        task = asyncio.create_task(self._run(job_id, record, urls, options, max_concurrency))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return job_id

    async def status(self, job_id: str) -> dict | None:
        record = await self.resources.io(self.resources.state.get, KEY + job_id)
        if record and record["status"] in UNFINISHED and time.time() > record["deadline_at"] + LOST_AFTER_SECONDS:
            return {**record, "status": "failed", "error": "Job lost: the process that ran it stopped"}
        return record

    async def rows(self, job_id: str, offset: int, limit: int) -> tuple[dict | None, list[dict]]:
        """The job's record, then up to limit of its rows from offset on.

        The record is read first. A job is marked done or failed only after its last row is
        written, so the rows read after a record that says so are complete, and an empty
        page then means the end. The page stops at the first row that does not exist rather
        than at the record's count: that is saved at most every PROGRESS_EVERY_SECONDS, and
        after a crash it trails the rows for good.

        That holds for an end the record states. A job reported lost is inferred from the
        clock (see status), and a process that stalled rather than stopped can still add rows
        to it."""
        record = await self.status(job_id)
        if record is None:
            return None, []
        state = self.resources.state

        def read():
            page = []
            for seq in range(offset, offset + limit):
                row = state.get(_row_key(job_id, seq))
                if row is None:
                    break
                page.append(row)
            return page

        return record, await self.resources.io(read)

    async def close(self):
        """Cancel running jobs and record why; a job cancelled before it started never ran its handler."""
        pending = dict(self.tasks)
        tasks = [task for task in pending.values() if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job_id in pending:
            record = await self.resources.io(self.resources.state.get, KEY + job_id)
            if record and record["status"] in UNFINISHED:
                record.update(status="failed", error="Interrupted: the service shut down", finished_at=_now())
                await self._settle(job_id, record)

    async def _run(self, job_id, record, urls, options, max_concurrency):
        record["status"] = "running"
        await self._save(job_id, record)
        written = time.monotonic()
        order = asyncio.Lock()

        async def deliver(position, item):
            nonlocal written
            async with order:  # numbered as they finish, so the stored rows are a gap-free prefix
                done = record["progress"]["done"]
                row = {"position": position, **item.model_dump(mode="json")}
                await self._write_row(job_id, done, row, self._keep(record))
                record["progress"] = {
                    "done": done + 1,
                    "succeeded": record["progress"]["succeeded"] + item.success,
                    "total": len(urls),
                }
            if time.monotonic() - written < PROGRESS_EVERY_SECONDS:
                return
            written = time.monotonic()
            try:
                await self._save(job_id, record)
            except Exception as exc:
                # Progress is incidental; the rows are the job. A busy state store must not
                # turn a batch that succeeded into a failure - the rule B08 set for metrics.
                # The record keeps the latest count, and the end save writes it.
                logger.warning("Job progress not recorded ({})", type(exc).__name__)

        try:
            await self.resources.service.stream_batch(urls, options, max_concurrency, deliver)
            record["status"] = "done"
        except Exception as exc:
            logger.error("Job failed ({})", type(exc).__name__)
            record.update(status="failed", error=f"Job failed ({type(exc).__name__})")
        record["finished_at"] = _now()
        await self._settle(job_id, record)

    async def _write_row(self, job_id, seq, row, keep):
        """Not caught: a result that cannot be stored fails the job."""
        await self.resources.io(self.resources.state.set, _row_key(job_id, seq), row, expire=keep)

    async def _settle(self, job_id, record):
        """Save the record of a job that has ended, and let its rows expire with it.

        A row's expiry was set when it was written, from the job's deadline. The rows of a job
        that ended early would otherwise stay on disk long after their record, and those of
        one that ran past its deadline would expire first - an empty first page from a job
        that says done. Record and rows get one absolute expiry, and the record is written
        last, so one that says the job ended finds its rows already settled."""
        state = self.resources.state
        until = time.time() + self._keep(record)

        def settle():
            seq = 0
            while state.touch(_row_key(job_id, seq), expire=until - time.time()):
                seq += 1
            state.set(KEY + job_id, record, expire=until - time.time())

        await self.resources.io(settle)

    def _keep(self, record):
        """Seconds a record or row stays. Unfinished work outlives its deadline long enough to
        be reported as lost; a row written while its job runs expires with the record."""
        keep = self.config.job_result_ttl
        if record["status"] in UNFINISHED:
            keep += max(0, record["deadline_at"] - time.time())
        return keep

    async def _save(self, job_id, record):
        await self.resources.io(self.resources.state.set, KEY + job_id, record, expire=self._keep(record))
