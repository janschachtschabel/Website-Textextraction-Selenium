"""Admission applies to each URL, including URLs inside a batch."""

import asyncio
from contextlib import asynccontextmanager

from .deadline import Deadline
from .results import CrawlError


class Capacity:
    def __init__(self, limit: int, max_queue: int, queue_timeout: float):
        self.semaphore = asyncio.Semaphore(limit)
        self.limit = limit
        self.max_queue = max_queue
        self.queue_timeout = queue_timeout
        self.active = 0
        self.waiting = 0

    @asynccontextmanager
    async def slot(self, deadline: Deadline):
        queued = self.semaphore.locked()
        if queued and self.waiting >= self.max_queue:
            raise CrawlError("Crawl queue is full", 503)
        acquired = False
        self.waiting += queued
        try:
            try:
                async with asyncio.timeout(min(deadline.remaining(), self.queue_timeout)):
                    await self.semaphore.acquire()
                    acquired = True
            except TimeoutError as exc:
                raise CrawlError("Crawl queue deadline exceeded", 504) from exc
        finally:
            self.waiting -= queued
        try:
            self.active += 1
            deadline.remaining()
            yield
        finally:
            if acquired:
                self.active -= 1
                self.semaphore.release()

    def stats(self):
        return {"active": self.active, "waiting": self.waiting, "limit": self.limit, "max_queue": self.max_queue}
