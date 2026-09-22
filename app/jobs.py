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
# Progress is written at most this often: a 2000-URL job would otherwise mean 2000 writes
# to a store shared across processes. _run saves once more at the end, so the final count
# is never the one the throttle swallowed.
PROGRESS_EVERY_SECONDS = 2.0
UNFINISHED = {"queued", "running"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
            "result": None,
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
                await self._save(job_id, record)

    async def _run(self, job_id, record, urls, options, max_concurrency):
        record["status"] = "running"
        await self._save(job_id, record)
        written = time.monotonic()

        async def report(done, succeeded):
            nonlocal written
            record["progress"] = {"done": done, "succeeded": succeeded, "total": len(urls)}
            if time.monotonic() - written < PROGRESS_EVERY_SECONDS:
                return
            written = time.monotonic()
            try:
                await self._save(job_id, record)
            except Exception as exc:
                # Progress is incidental; the crawling is the job. A busy state store must
                # not turn a batch that succeeded into a failure - the rule B08 set for
                # metrics. The record keeps the latest count, and the end save writes it.
                logger.warning("Job progress not recorded ({})", type(exc).__name__)

        try:
            result = await self.resources.service.crawl_batch(urls, options, max_concurrency, report)
            record.update(status="done", result=result.model_dump(mode="json"))
        except Exception as exc:
            logger.error("Job failed ({})", type(exc).__name__)
            record.update(status="failed", error=f"Job failed ({type(exc).__name__})")
        record["finished_at"] = _now()
        await self._save(job_id, record)

    async def _save(self, job_id, record):
        # Unfinished records outlive their deadline long enough to be reported as lost.
        keep = self.config.job_result_ttl
        if record["status"] in UNFINISHED:
            keep += max(0, record["deadline_at"] - time.time())
        await self.resources.io(self.resources.state.set, KEY + job_id, record, expire=keep)
