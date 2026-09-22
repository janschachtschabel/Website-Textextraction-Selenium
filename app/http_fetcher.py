"""One bounded streaming path, with guarded redirects and no shared cookie jar."""

import asyncio
import ssl
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from .body_reader import read_body
from .deadline import Deadline
from .results import CrawlError, FetchResult
from .schemas import CrawlOptions
from .security import resolve_target

RETRY_STATUSES = {429, 500, 502, 503, 504}
# RFC 9110: these carry no body, but keep the headers of the representation they stand for,
# Content-Encoding among them. Reading one as a compressed stream finds no end of one.
BODILESS_STATUSES = {204, 304}
# Transport-level requests carry no client defaults: without Accept some servers answer 406.
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
VALIDATORS = {"etag": ("etag", "If-None-Match"), "last_modified": ("last-modified", "If-Modified-Since")}


def response_validators(headers, url: str) -> dict[str, str]:
    # Echoed back in a later request: only short printable values, or revalidation would fail forever.
    found = {
        name: headers[header]
        for name, (header, _) in VALIDATORS.items()
        if header in headers and len(headers[header]) <= 512 and all(32 <= ord(c) <= 126 for c in headers[header])
    }
    # Bound to the issuing URL: across a redirect another site must not receive them (ETags can track).
    return {**found, "url": url} if found else {}


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return min(30, max(0, float(value)))
        except ValueError:
            try:
                return min(30, max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                pass  # Invalid Retry-After: use bounded exponential backoff.
    return min(2**attempt, 8)


def _request(url, options, validators, remaining):
    """The outgoing GET. Conditional only when the validators belong to this very URL: a
    redirect may leave the site that issued them, and an ETag can identify a visitor."""
    headers = {"User-Agent": options.user_agent, "Accept": ACCEPT, "Accept-Encoding": "gzip, deflate"}
    if options.accept_language:
        headers["Accept-Language"] = options.accept_language
    if validators.get("url") == url:
        for name, (_, conditional) in VALIDATORS.items():
            if name in validators:
                headers[conditional] = validators[name]
    return httpx.Request(
        "GET",
        url,
        headers=headers,
        extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), remaining)},
    )


class HTTPFetcher:
    def __init__(
        self, proxy_url: str | None = None, *, transport=None, validate=resolve_target, acquire=None, max_connections=16
    ):
        if transport is None and proxy_url is None:
            raise ValueError("A guarded egress proxy is required")
        self.validate = validate
        self.acquire = acquire
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
        self.transports = (
            {False: transport, True: transport}
            if transport
            else {
                False: httpx.AsyncHTTPTransport(
                    proxy=proxy_url, verify=ssl.create_default_context(), http2=True, limits=limits, trust_env=False
                ),
                True: httpx.AsyncHTTPTransport(
                    proxy=proxy_url, verify=False, http2=True, limits=limits, trust_env=False
                ),
            }
        )

    async def close(self):
        for transport in set(self.transports.values()):
            await transport.aclose()

    async def fetch(self, url: str, options: CrawlOptions, deadline: Deadline, validators=None) -> FetchResult:
        """Conditional when ``validators`` (from an earlier FetchResult) are given: 304 means unchanged."""
        return await deadline.run(self._fetch(url, options, deadline, validators or {}))

    async def _fetch(self, url, options, deadline, validators):
        for attempt in range(options.retries + 1):
            try:
                result, after = await self._redirects(url, options, deadline, validators)
                if result.status_code not in RETRY_STATUSES or attempt == options.retries:
                    return result
            except httpx.TimeoutException as exc:
                # Each HTTPX timeout is the remaining deadline; a coarse clock can let it fire first.
                raise CrawlError("Crawl deadline exceeded", 504) from exc
            except httpx.HTTPError as exc:
                if attempt == options.retries:
                    raise CrawlError("HTTP download failed") from exc
                after = None
            await deadline.run(asyncio.sleep(retry_delay(after, attempt)))
        raise AssertionError("Unreachable retry state")

    async def _redirects(self, url, options, deadline, validators):
        visited = set()
        for _ in range(11):
            if url in visited:
                raise CrawlError("Redirect loop detected")
            visited.add(url)
            await deadline.run(asyncio.to_thread(self.validate, url))
            if self.acquire:
                await self.acquire(url, options.crawl_rate_limit_rps, deadline)
            request = _request(url, options, validators, deadline.remaining())
            response = await self.transports[options.allow_insecure_ssl].handle_async_request(request)
            try:
                if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code in BODILESS_STATUSES:
                    data, truncated = b"", False
                else:
                    data, truncated = await read_body(response, options.max_bytes)
                result = FetchResult(
                    data,
                    str(request.url),
                    response.status_code,
                    response.headers.get("content-type"),
                    truncated=truncated,
                    validators=response_validators(response.headers, str(request.url)),
                )
                if truncated:
                    result.warnings.append("Response truncated at max_bytes")
                return result, response.headers.get("retry-after")
            finally:
                await response.aclose()
        raise CrawlError("Too many redirects")
