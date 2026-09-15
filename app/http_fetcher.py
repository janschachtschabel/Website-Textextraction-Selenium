"""One bounded streaming path, with guarded redirects and no shared cookie jar."""
import asyncio
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from .deadline import Deadline
from .results import CrawlError, FetchResult
from .schemas import CrawlOptions
from .security import resolve_target

RETRY_STATUSES = {429, 500, 502, 503, 504}


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return min(30, max(0, float(value)))
        except ValueError:
            try:
                return min(30, max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                pass  # Invalid Retry-After: use bounded exponential backoff.
    return min(2 ** attempt, 8)


class HTTPFetcher:
    def __init__(self, proxy_url: str | None = None, *, transport=None, validate=resolve_target, acquire=None, max_connections=16):
        if transport is None and proxy_url is None:
            raise ValueError('A guarded egress proxy is required')
        self.validate = validate
        self.acquire = acquire
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
        self.transports = {False: transport, True: transport} if transport else {
            False: httpx.AsyncHTTPTransport(proxy=proxy_url, verify=ssl.create_default_context(), http2=True, limits=limits, trust_env=False),
            True: httpx.AsyncHTTPTransport(proxy=proxy_url, verify=False, http2=True, limits=limits, trust_env=False),
        }

    async def close(self):
        for transport in set(self.transports.values()):
            await transport.aclose()

    async def fetch(self, url: str, options: CrawlOptions, deadline: Deadline) -> FetchResult:
        return await deadline.run(self._fetch(url, options, deadline))

    async def _fetch(self, url, options, deadline):
        for attempt in range(options.retries + 1):
            try:
                result, after = await self._redirects(url, options, deadline)
                if result.status_code not in RETRY_STATUSES or attempt == options.retries:
                    return result
            except httpx.HTTPError as exc:
                if attempt == options.retries:
                    raise CrawlError('HTTP download failed') from exc
                after = None
            await deadline.run(asyncio.sleep(retry_delay(after, attempt)))
        raise AssertionError('Unreachable retry state')

    async def _redirects(self, url, options, deadline):
        visited = set()
        for _ in range(11):
            if url in visited:
                raise CrawlError('Redirect loop detected')
            visited.add(url)
            await deadline.run(asyncio.to_thread(self.validate, url))
            if self.acquire:
                await self.acquire(url, options.crawl_rate_limit_rps, deadline)
            remaining = deadline.remaining()
            request = httpx.Request('GET', url, headers={'User-Agent': options.user_agent, 'Accept-Encoding': 'identity'},
                                    extensions={'timeout': dict.fromkeys(('connect', 'read', 'write', 'pool'), remaining)})
            response = await self.transports[options.allow_insecure_ssl].handle_async_request(request)
            try:
                if response.status_code in {301, 302, 303, 307, 308} and response.headers.get('location'):
                    url = urljoin(url, response.headers['location'])
                    continue
                buf = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    available = options.max_bytes - len(buf)
                    buf.extend(chunk[:available])
                    if len(chunk) > available:
                        truncated = True
                        break
                result = FetchResult(bytes(buf), str(request.url), response.status_code,
                                     response.headers.get('content-type'), truncated=truncated)
                if truncated:
                    result.warnings.append('Response truncated at max_bytes')
                return result, response.headers.get('retry-after')
            finally:
                await response.aclose()
        raise CrawlError('Too many redirects')
