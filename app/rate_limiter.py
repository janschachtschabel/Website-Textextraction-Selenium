"""Process-shared reservations for document attempts (not browser asset requests)."""
import asyncio
import time
from urllib.parse import urlsplit

from .config import Settings
from .deadline import Deadline
from .results import CrawlError


class RateLimiter:
    def __init__(self, store, config: Settings):
        self.store = store
        self.global_rps = config.global_rate_limit_rps

    def reserve(self, domain: str, rps: float, *, now: float, latest: float) -> float:
        limits = [('rate:global', self.global_rps), ('rate:' + domain, rps)]
        active = [(key, 1 / rate) for key, rate in limits if rate > 0]
        with self.store.transact():
            slot = max([now] + [self.store.get(key, default=now - interval) + interval for key, interval in active])
            if slot > latest:
                raise CrawlError('Crawl deadline exceeded while waiting for rate limit', 504)
            for key, interval in active:
                self.store.set(key, slot, expire=max(3600, slot - now + interval + 60))
        return slot

    async def acquire(self, url: str, rps: float, deadline: Deadline):
        now = time.time()
        slot = await deadline.run(asyncio.to_thread(self.reserve, urlsplit(url).hostname, rps,
                                                   now=now, latest=now + deadline.remaining()))
        await deadline.run(asyncio.sleep(max(0, slot - time.time())))
