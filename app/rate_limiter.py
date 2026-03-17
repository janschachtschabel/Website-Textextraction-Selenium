"""
Rate limiting for outbound crawl requests.

Two independent layers:
  - Global limiter : caps total crawl throughput across all domains (req/s).
  - Per-domain limiter : enforces a polite crawl delay per target hostname (req/s).

Both layers are disabled by default (0 = off).
Per-request override via `crawl_rate_limit_rps` takes precedence over the
server-wide DEFAULT_DOMAIN_RATE_LIMIT_RPS for that specific domain.

Note: if a domain's rps is set differently across calls, the value from the
*first* call wins for the lifetime of the cached limiter (TTL=1h).
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from aiolimiter import AsyncLimiter
from cachetools import TTLCache
from loguru import logger

from .config import settings

_DOMAIN_TTL = 3600       # drop idle per-domain limiters after 1 h
_DOMAIN_MAX = 1_000      # max distinct domains tracked simultaneously

# Module-level state — populated by init_rate_limiters() during lifespan startup
_global_limiter: AsyncLimiter | None = None
_domain_limiters: TTLCache[str, AsyncLimiter] = TTLCache(
    maxsize=_DOMAIN_MAX, ttl=_DOMAIN_TTL
)
_domain_lock = asyncio.Lock()


def init_rate_limiters() -> None:
    """Initialise rate limiters from current settings.  Call once at startup."""
    global _global_limiter
    rps = settings.global_rate_limit_rps
    if rps > 0:
        _global_limiter = AsyncLimiter(max_rate=rps, time_period=1.0)
        logger.info(f"Global rate limiter: {rps:.1f} req/s")
    else:
        _global_limiter = None
        logger.debug("Global rate limiter disabled (GLOBAL_RATE_LIMIT_RPS=0)")

    drps = settings.default_domain_rate_limit_rps
    if drps > 0:
        logger.info(f"Default per-domain rate limit: {drps:.1f} req/s")
    else:
        logger.debug("Per-domain rate limiter disabled by default (DEFAULT_DOMAIN_RATE_LIMIT_RPS=0)")


async def _get_domain_limiter(domain: str, rps: float) -> AsyncLimiter:
    """Return (or create) an AsyncLimiter for *domain* at *rps* req/s."""
    async with _domain_lock:
        limiter = _domain_limiters.get(domain)
        if limiter is None:
            limiter = AsyncLimiter(max_rate=rps, time_period=1.0)
            _domain_limiters[domain] = limiter
        return limiter


async def acquire(url: str, domain_rps_override: float | None = None) -> None:
    """Wait for rate-limit slots before starting a crawl.

    Args:
        url: The target URL (used to extract the hostname for per-domain limiting).
        domain_rps_override: Per-request rps override.  When provided, it replaces
            the server default for this domain.  Pass ``None`` to use the server
            default (DEFAULT_DOMAIN_RATE_LIMIT_RPS).  Pass ``0.0`` to disable
            per-domain limiting for this specific request.
    """
    # ── Global slot ───────────────────────────────────────────────────────────
    if _global_limiter is not None:
        await _global_limiter.acquire()

    # ── Per-domain slot ───────────────────────────────────────────────────────
    effective_rps = (
        domain_rps_override
        if domain_rps_override is not None
        else settings.default_domain_rate_limit_rps
    )
    if effective_rps > 0:
        hostname = urlparse(url).hostname or url
        limiter = await _get_domain_limiter(hostname, effective_rps)
        await limiter.acquire()
        logger.debug(f"Rate-limit slot acquired for {hostname} ({effective_rps:.1f} req/s)")
