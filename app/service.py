"""Shared per-URL pipeline for both API endpoints."""

import asyncio
import time
from functools import partial
from urllib.parse import urlsplit

from loguru import logger

from .capacity import Capacity
from .deadline import Deadline
from .preflight import blocked_content
from .request_id import current_request_id
from .result_cache import make_cache_key
from .results import CrawlError
from .robots import RobotsPolicy
from .schemas import BatchCrawlItemResult, BatchCrawlResponse, CrawlResponse
from .worker_tasks import anonymize_document, prepare_document

STALE = "stale:"  # result plus ETag/Last-Modified, kept for REVALIDATION_TTL after the fresh entry


def failure_reason(result: CrawlResponse) -> str:
    """Why success is false. Mirrors the rule in CrawlService._extract; change both together."""
    # The upstream status comes first: a 4xx body is classified "blocked", which would
    # otherwise hide the status that actually explains the failure.
    if result.status_code is None:
        return "Upstream status unknown"
    if not 200 <= result.status_code < 300:
        return f"Upstream status {result.status_code}"
    if result.extraction_status != "ok":
        return f"Extraction {result.extraction_status}"
    if result.truncated:
        return "Extraction truncated"
    return "Extraction incomplete"  # e.g. a rendered page that never settled; see warnings


class CrawlService:
    def __init__(self, resources):
        self.resources = resources
        self.config = resources.config
        self.capacity = Capacity(
            self.config.max_concurrent_requests, self.config.max_queue_size, self.config.queue_timeout_seconds
        )
        self.inflight = {}
        self.robots = RobotsPolicy(resources)

    async def crawl(self, url, options, deadline=None):
        started = time.monotonic()
        deadline = deadline or Deadline(options.timeout_ms / 1000)
        result = None
        try:
            result = await deadline.run(self._cached(url, options, deadline))
            result.elapsed_ms = round((time.monotonic() - started) * 1000)
            if not result.success:
                self._log_failure(
                    url, options, started, failure_reason(result), result.status_code, result.extraction_status
                )
            return result
        except CrawlError as exc:
            self._log_failure(url, options, started, str(exc), exc.status_code)
            raise
        except Exception as exc:
            error = CrawlError("Unexpected extraction failure", 502)
            self._log_failure(url, options, started, f"{error} ({type(exc).__name__})", 502, level="ERROR")
            raise error from exc
        finally:
            await self._record(
                time.monotonic() - started,
                bool(result and result.success),
                bool(result and result.cached),
                bool(result and result.coalesced),
            )

    async def _record(self, elapsed, success, cached=False, coalesced=False):
        """Recording runs in a finally: a busy or unwritable store must not discard the answer."""
        try:
            await self.resources.metrics.record(elapsed, success, cached, coalesced)
        except Exception as exc:
            logger.warning("Metrics not recorded ({})", type(exc).__name__)

    async def crawl_batch(self, urls, options, max_concurrency, on_progress=None) -> BatchCrawlResponse:
        started = time.monotonic()
        # All deadlines start at batch admission, including time behind max_concurrency.
        expires_at = started + options.timeout_ms / 1000
        semaphore = asyncio.Semaphore(max_concurrency)
        tally = {"done": 0, "succeeded": 0}

        async def counted(url):
            """The caller hears about each finished URL. Counting here rather than inside
            _batch_item keeps the item's own error handling in one piece."""
            item = await self._batch_item(url, options, expires_at, semaphore, started)
            tally["done"] += 1
            tally["succeeded"] += item.success
            await on_progress(tally["done"], tally["succeeded"])
            return item

        each = counted if on_progress else lambda url: self._batch_item(url, options, expires_at, semaphore, started)
        results = await asyncio.gather(*(each(url) for url in urls))
        succeeded = sum(item.success for item in results)
        return BatchCrawlResponse(
            total=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=results,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    async def stream_batch(self, urls, options, max_concurrency, deliver) -> None:
        """Crawl like crawl_batch, but hand each item to deliver(position, item) as it
        finishes instead of collecting it, so a job holds max_concurrency results at a time
        rather than all of them. A delivery that fails fails the batch and cancels the URLs
        still pending: their results would have nowhere to go."""
        started = time.monotonic()
        # All deadlines start at batch admission, including time behind max_concurrency.
        expires_at = started + options.timeout_ms / 1000
        semaphore = asyncio.Semaphore(max_concurrency)
        try:
            async with asyncio.TaskGroup() as group:
                for position, url in enumerate(urls):
                    handoff = partial(deliver, position)
                    group.create_task(self._batch_item(url, options, expires_at, semaphore, started, handoff))
        except ExceptionGroup as failed:
            raise failed.exceptions[0] from failed

    async def _batch_item(self, url, options, expires_at, semaphore, started, deliver=None):
        deadline = Deadline.at(expires_at)
        acquired = False
        try:
            try:
                await deadline.run(semaphore.acquire())
                acquired = True
                result = await self.crawl(url, options, deadline)
                item = BatchCrawlItemResult(
                    url=url,
                    success=result.success,
                    result=result,
                    error=None if result.success else failure_reason(result),
                )
            except CrawlError as exc:
                if not acquired:
                    # crawl() records the attempt itself; a queue failure never reaches it.
                    await self._record(time.monotonic() - started, False)
                item = BatchCrawlItemResult(url=url, success=False, error=str(exc))
            if deliver is None:
                return item
            # Still inside the slot, so a slow result store holds the batch back instead of
            # letting finished results pile up in memory behind it.
            await deliver(item)
            return None
        finally:
            if acquired:
                semaphore.release()

    def _log_failure(self, url, options, started, reason, status, extraction=None, level="WARNING"):
        # Host only: paths and query strings can carry tokens or personal data.
        logger.log(
            level,
            "Crawl failed: {reason} (host={host} mode={mode} status={status} "
            "extraction={extraction} elapsed_ms={elapsed_ms} request={request_id})",
            reason=reason,
            host=urlsplit(url).hostname,
            mode=options.mode,
            status=status,
            extraction=extraction,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            request_id=current_request_id(),
        )

    async def _cached(self, url, options, deadline):
        resources = self.resources
        key = make_cache_key(url, options, self.config)
        if self.config.result_cache_ttl and not options.force_refresh:
            cached = await resources.io(resources.cache.get, key)
            if cached is not None:
                return CrawlResponse.model_validate(cached).model_copy(update={"cached": True, "coalesced": False})
        if key in self.inflight and not options.force_refresh:
            result = await deadline.run(self._joined(key))
            return result.model_copy(deep=True, update={"coalesced": True})
        future = asyncio.get_running_loop().create_future()
        # A leader can fail without followers; retrieve the exception in that case too.
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        # A forced refresh fetches beside the leader rather than displacing it.
        self.inflight.setdefault(key, future)
        try:
            stale = None
            if self.config.result_cache_ttl and self.config.revalidation_ttl and not options.force_refresh:
                stale = await resources.io(resources.cache.get, STALE + key)
            async with self.capacity.slot(deadline):
                result, validators = await self._extract(url, options, deadline, stale)
            if result.success and self.config.result_cache_ttl:
                await self._store(key, result, validators)
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
            if self.inflight.get(key) is future:
                del self.inflight[key]

    async def _joined(self, key):
        # A coroutine, so an expired deadline closes it instead of dropping an unawaited shield.
        return await asyncio.shield(self.inflight[key])

    async def _store(self, key, result, validators):
        resources = self.resources
        stored = result.model_copy(update={"cached": False, "coalesced": False, "revalidated": False})
        stored = stored.model_dump(mode="json")
        await resources.io(resources.cache.set, key, stored, expire=self.config.result_cache_ttl)
        if validators and self.config.revalidation_ttl:
            entry = {"validators": validators, "result": stored}
            await resources.io(resources.cache.set, STALE + key, entry, expire=self.config.revalidation_ttl)

    async def _extract(self, url, options, deadline, stale=None):
        """The response and, for an HTTP-engine result, the upstream validators to revalidate it with."""
        resources = self.resources
        if options.respect_robots_txt and not await self.robots.allowed(url, options, deadline):
            raise CrawlError("Disallowed by robots.txt", 403)
        if options.mode == "js":
            await deadline.run(asyncio.to_thread(resources.validate, url))
            fetched = await resources.browser.fetch(url, options, deadline)
        else:
            fetched = await resources.fetch_http(url, options, deadline, stale["validators"] if stale else None)
            if stale and fetched.status_code == 304:
                result = CrawlResponse.model_validate(stale["result"])
                return result.model_copy(update={"cached": True, "revalidated": True}), stale["validators"]
        converted, use_browser, links, metadata = await resources.conversion_pool.run(
            prepare_document, (fetched, options, deadline.expires_at), deadline
        )
        if use_browser:  # prepare_document only proposes this in auto mode
            fetched = await resources.browser.fetch(fetched.final_url, options, deadline)
            converted, _, links, metadata = await resources.conversion_pool.run(
                prepare_document, (fetched, options, deadline.expires_at), deadline
            )
        blocked = blocked_content(converted.markdown, fetched.status_code)
        warnings = fetched.warnings + converted.warnings
        anon = None
        if options.anonymize:
            converted.markdown, anon = await resources.conversion_pool.run(
                anonymize_document, (converted.markdown, options.anonymize_language), deadline
            )
            links = metadata = None
            fetched.screenshot_base64 = None
            if options.extract_links or options.extract_metadata or options.screenshot:
                warnings.append("Links, metadata and screenshots are suppressed for anonymized responses")
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
        response = CrawlResponse(
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
            metadata=metadata,
            screenshot_base64=fetched.screenshot_base64,
            anonymization=anon,
            elapsed_ms=0,
        )
        # A rendered result can change although the document did not, so only HTTP results revalidate.
        return response, fetched.validators if fetched.engine == "http" else {}
