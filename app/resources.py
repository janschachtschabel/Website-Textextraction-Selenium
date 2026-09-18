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

# Tracks the on-disk layout (JSON values, one directory per store). Result identity is
# versioned separately in result_cache.CACHE_VERSION; the two change for different reasons.
STORAGE_LAYOUT = "json-v1"


def private_to_owner(info, uid: int) -> bool:
    return info.st_uid == uid and not info.st_mode & 0o077


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
        # mkdir's mode only applies to a directory this process creates.
        if os.name == "posix" and not private_to_owner(base.stat(), os.getuid()):
            raise RuntimeError(f"{base} must be private to the service user (mode 0700)")
        # JSONDisk instead of the default pickle: whoever can write here would otherwise
        # execute code in this process when a cached result is read (CVE-2025-69872).
        self.cache = diskcache.Cache(
            str(base / f"results-{STORAGE_LAYOUT}"),
            disk=diskcache.JSONDisk,
            size_limit=self.config.result_cache_max_size * 1024 * 1024,
        )
        try:
            self.state = diskcache.Cache(
                str(base / f"state-{STORAGE_LAYOUT}"), disk=diskcache.JSONDisk, eviction_policy="none"
            )
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
            self.browser_pool = WorkerPool(self.config.selenium_max_pool_size, self.config.worker_max_jobs)
            self.conversion_pool = WorkerPool(self.config.conversion_workers, self.config.worker_max_jobs)
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

    async def fetch_http(self, url, options, deadline, validators=None):
        if not options.proxy:
            return await self.http.fetch(url, options, deadline, validators)
        async with EgressProxy(protection=self.config.ssrf_protection, upstream=options.proxy) as guard:
            fetcher = HTTPFetcher(
                guard.url,
                validate=self.validate,
                acquire=self.rate.acquire,
                max_connections=self.config.http_max_connections,
            )
            try:
                return await fetcher.fetch(url, options, deadline, validators)
            finally:
                await fetcher.close()
