"""An absolute deadline shared by queueing, fetches and worker execution."""
import asyncio
import time

from .results import CrawlError


class Deadline:
    def __init__(self, seconds: float):
        self.expires_at = time.monotonic() + seconds

    @classmethod
    def at(cls, expires_at: float):
        deadline = cls(0)
        deadline.expires_at = expires_at
        return deadline

    def remaining(self) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise CrawlError('Crawl deadline exceeded', 504)
        return remaining

    async def run(self, awaitable):
        # Enter the timeout before awaiting, but always close an unstarted coroutine.
        try:
            remaining = self.remaining()
        except CrawlError:
            if hasattr(awaitable, 'close'):
                awaitable.close()
            raise
        try:
            async with asyncio.timeout(remaining):
                return await awaitable
        except TimeoutError as exc:
            raise CrawlError('Crawl deadline exceeded', 504) from exc
