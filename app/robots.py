"""Opt-in robots.txt check (RFC 9309) for the requested URL of a crawl."""

from urllib.parse import urlsplit

from protego import Protego

from .markup import decode_text
from .results import CrawlError

ROBOTS_MAX_BYTES = 512 * 1024  # RFC 9309 asks crawlers to parse at least 500 KiB
KEEP_SECONDS = 3600  # a fetched or unavailable robots.txt, per origin
RETRY_SECONDS = 300  # an unreachable one counts as a complete disallow for this long


class RobotsPolicy:
    """Answers from a per-origin robots.txt, fetched through the guarded HTTP path.

    Python's urllib.robotparser applies the first matching rule; RFC 9309 wants the
    longest, so a page allowed below a broader Disallow would be refused. Protego follows
    the RFC, including product-token groups and wildcards.
    """

    def __init__(self, resources):
        self.resources = resources

    async def allowed(self, url, options, deadline) -> bool:
        resources = self.resources
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rules = await resources.io(resources.cache.get, "robots:" + origin)
        if rules is None:
            rules, keep = await self._fetch(origin, options, deadline)
            await resources.io(resources.cache.set, "robots:" + origin, rules, expire=keep)
        if "all" in rules:
            return rules["all"]
        return Protego.parse(rules["text"]).can_fetch(url, options.user_agent)

    async def _fetch(self, origin, options, deadline):
        robots_options = options.model_copy(update={"max_bytes": ROBOTS_MAX_BYTES})
        try:
            fetched = await self.resources.fetch_http(origin + "/robots.txt", robots_options, deadline)
        except CrawlError as exc:
            if exc.status_code != 502:
                raise  # an expired deadline or a blocked target fails the crawl itself
            return {"all": False}, RETRY_SECONDS  # unreachable
        status = fetched.status_code or 0
        if 200 <= status < 300:
            return {"text": decode_text(fetched.data, fetched.content_type)}, KEEP_SECONDS
        if 400 <= status < 500:
            return {"all": True}, KEEP_SECONDS  # unavailable: no restrictions
        return {"all": False}, RETRY_SECONDS  # server error: unreachable
