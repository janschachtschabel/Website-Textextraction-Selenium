"""Shared per-URL pipeline for both API endpoints."""

import asyncio
import time

from loguru import logger

from .capacity import Capacity
from .deadline import Deadline
from .preflight import blocked_content
from .result_cache import make_cache_key
from .results import CrawlError
from .schemas import CrawlResponse
from .worker_tasks import anonymize_document, prepare_document


class CrawlService:
    def __init__(self, resources):
        self.resources = resources
        self.config = resources.config
        self.capacity = Capacity(
            self.config.max_concurrent_requests, self.config.max_queue_size, self.config.queue_timeout_seconds
        )
        self.inflight = {}

    async def crawl(self, url, options, deadline=None):
        started = time.monotonic()
        deadline = deadline or Deadline(options.timeout_ms / 1000)
        result = None
        try:
            result = await deadline.run(self._cached(url, options, deadline))
            result.elapsed_ms = round((time.monotonic() - started) * 1000)
            return result
        except CrawlError:
            raise
        except Exception as exc:
            logger.error("Unexpected extraction failure ({})", type(exc).__name__)
            raise CrawlError("Unexpected extraction failure", 502) from exc
        finally:
            await self.resources.metrics.record(
                time.monotonic() - started,
                bool(result and result.success),
                bool(result and result.cached),
                bool(result and result.coalesced),
            )

    async def _cached(self, url, options, deadline):
        resources = self.resources
        key = make_cache_key(url, options, self.config)
        if self.config.result_cache_ttl and not options.force_refresh:
            cached = await resources.io(resources.cache.get, key)
            if cached is not None:
                return CrawlResponse.model_validate(cached).model_copy(update={"cached": True, "coalesced": False})
        if key in self.inflight:
            result = await deadline.run(asyncio.shield(self.inflight[key]))
            return result.model_copy(deep=True, update={"coalesced": True})
        future = asyncio.get_running_loop().create_future()
        # A leader can fail without followers; retrieve the exception in that case too.
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        self.inflight[key] = future
        try:
            async with self.capacity.slot(deadline):
                result = await self._extract(url, options, deadline)
            if result.success and self.config.result_cache_ttl:
                await resources.io(resources.cache.set, key, result.model_dump(), expire=self.config.result_cache_ttl)
            future.set_result(result)
            return result
        except BaseException as exc:
            public_error = (
                exc
                if isinstance(exc, CrawlError)
                else (
                    CrawlError("Crawl interrupted", 504)
                    if isinstance(exc, asyncio.CancelledError)
                    else CrawlError("Unexpected extraction failure", 502)
                )
            )
            future.set_exception(public_error)
            raise
        finally:
            self.inflight.pop(key, None)

    async def _extract(self, url, options, deadline):
        resources = self.resources
        if options.mode == "js":
            await deadline.run(asyncio.to_thread(resources.validate, url))
            fetched = await resources.browser.fetch(url, options, deadline)
        else:
            fetched = await resources.fetch_http(url, options, deadline)
        converted, use_browser, links = await resources.conversion_pool.run(
            prepare_document, (fetched, options, deadline.expires_at), deadline
        )
        if options.mode == "auto" and use_browser:
            fetched = await resources.browser.fetch(fetched.final_url, options, deadline)
            converted, _, links = await resources.conversion_pool.run(
                prepare_document, (fetched, options, deadline.expires_at), deadline
            )
        blocked = blocked_content(converted.markdown, fetched.status_code)
        warnings = fetched.warnings + converted.warnings
        anon = None
        if options.anonymize:
            converted.markdown, anon = await resources.conversion_pool.run(
                anonymize_document, (converted.markdown, options.anonymize_language), deadline
            )
            links = None
            fetched.screenshot_base64 = None
            if options.extract_links or options.screenshot:
                warnings.append("Links and screenshots suppressed for anonymized responses")
        if options.screenshot and fetched.engine == "http":
            warnings.append("Screenshot unavailable on the HTTP path; use mode=js to require rendering")
        success = bool(
            converted.status == "ok"
            and converted.markdown.strip()
            and not blocked
            and not fetched.truncated
            and fetched.settled
            and fetched.status_code is not None
            and 200 <= fetched.status_code < 300
        )
        return CrawlResponse(
            request_mode=options.mode,
            fetch_engine=fetched.engine,
            converter=converted.converter,
            extraction_status="blocked" if blocked else converted.status,
            success=success,
            requested_url=url,
            final_url=fetched.final_url,
            status_code=fetched.status_code,
            redirected=url != fetched.final_url,
            content_type=fetched.content_type,
            markdown=converted.markdown,
            markdown_length=len(converted.markdown),
            word_count=len(converted.markdown.split()),
            error_page_detected=blocked or converted.status in {"empty", "failed"},
            truncated=fetched.truncated,
            warnings=warnings,
            links=links,
            screenshot_base64=fetched.screenshot_base64,
            anonymization=anon,
            elapsed_ms=0,
        )
