"""Application-owned resources, closed in reverse order on shutdown."""

import asyncio
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from pathlib import Path

import diskcache

from .egress_proxy import EgressProxy
from .http_fetcher import HTTPFetcher
from .js_fetcher import BrowserFetcher
from .metrics import Metrics
from .rate_limiter import RateLimiter
from .security import resolve_target
from .service import CrawlService
from .workers import WorkerPool


class Resources:
    def __init__(self, config, *, transport=None, validate=None, browser=None):
        self.config = config
        self.transport = transport
        self.validate = validate or functools.partial(resolve_target, protection=config.ssrf_protection)
        self.browser = browser
        self.ready = False

    async def io(self, function, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, functools.partial(function, *args, **kwargs)
        )

    def _open_stores(self):
        base = (
            Path(self.config.result_cache_dir)
            if self.config.result_cache_dir
            else Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "website-text-extraction"
        )
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache = diskcache.Cache(
            str(base / "results-v3"), size_limit=self.config.result_cache_max_size * 1024 * 1024
        )
        try:
            self.state = diskcache.Cache(str(base / "state-v3"), eviction_policy="none")
            self.state.expire()
        except BaseException:
            self.cache.close()
            raise

    async def _close_stores(self):
        await self.io(self.cache.close)
        await self.io(self.state.close)
        self.executor.shutdown(wait=True)

    async def __aenter__(self):
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        try:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extraction-storage")
            await self.io(self._open_stores)
            self.stack.push_async_callback(self._close_stores)
            guard = await self.stack.enter_async_context(EgressProxy(protection=self.config.ssrf_protection))
            self.rate = RateLimiter(self.state, self.config, run_sync=self.io)
            self.metrics = Metrics(self.state, self.io)
            self.http = HTTPFetcher(
                guard.url,
                transport=self.transport,
                validate=self.validate,
                acquire=self.rate.acquire,
                max_connections=self.config.http_max_connections,
            )
            self.stack.push_async_callback(self.http.close)
            self.browser_pool = WorkerPool(self.config.selenium_max_pool_size)
            self.conversion_pool = WorkerPool(self.config.conversion_workers)
            self.stack.push_async_callback(self.browser_pool.close)
            self.stack.push_async_callback(self.conversion_pool.close)
            self.browser = self.browser or BrowserFetcher(self.browser_pool, self.rate, self.config)
            self.service = CrawlService(self)
            self.ready = True
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        self.ready = False
        await self.stack.aclose()

    async def fetch_http(self, url, options, deadline):
        if not options.proxy:
            return await self.http.fetch(url, options, deadline)
        async with EgressProxy(protection=self.config.ssrf_protection, upstream=options.proxy) as guard:
            fetcher = HTTPFetcher(
                guard.url,
                validate=self.validate,
                acquire=self.rate.acquire,
                max_connections=self.config.http_max_connections,
            )
            try:
                return await fetcher.fetch(url, options, deadline)
            finally:
                await fetcher.close()
