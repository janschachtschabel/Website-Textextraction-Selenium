from __future__ import annotations

import asyncio
import ssl

import httpx

from .config import settings

try:
    import truststore
    _TRUSTSTORE_AVAILABLE = True
except ImportError:
    _TRUSTSTORE_AVAILABLE = False

# ---------------------------------------------------------------------------
# SSL context – created once at import time, reused for every request.
# truststore uses the OS certificate store (Windows CertStore / macOS Keychain
# / Linux system certs) so corporate/proxy CAs are trusted automatically.
# ---------------------------------------------------------------------------
def _build_ssl_context() -> bool | ssl.SSLContext:
    if _TRUSTSTORE_AVAILABLE:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return True  # fall back to certifi bundle

_SSL_CONTEXT: bool | ssl.SSLContext = _build_ssl_context()

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    # Some sites check these
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# ---------------------------------------------------------------------------
# Persistent httpx client – one instance for the entire process lifetime.
# Eliminates per-request TCP + TLS handshake and enables HTTP/2 multiplexing.
# A separate short-lived client is created only when allow_insecure_ssl=True.
# ---------------------------------------------------------------------------
_persistent_client: httpx.AsyncClient | None = None


def _make_client(verify: bool | ssl.SSLContext, proxy: str | None = None) -> httpx.AsyncClient:
    kwargs: dict = {
        "follow_redirects": True,
        "headers": DEFAULT_HEADERS,
        "limits": httpx.Limits(
            max_connections=200,
            max_keepalive_connections=40,
            keepalive_expiry=30,
        ),
        "http2": True,
        "verify": verify,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


async def init_http_client() -> None:
    """Create the persistent client. Call once from the FastAPI lifespan."""
    global _persistent_client
    _persistent_client = _make_client(_SSL_CONTEXT)


async def close_http_client() -> None:
    """Gracefully close the persistent client. Call once from the FastAPI lifespan."""
    global _persistent_client
    if _persistent_client is not None:
        await _persistent_client.aclose()
        _persistent_client = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared persistent client (must be initialised first)."""
    if _persistent_client is None:
        raise RuntimeError("HTTP client not initialised – call init_http_client() in lifespan")
    return _persistent_client


# Status codes that warrant a retry (server-side transient errors + rate-limit)
_RETRY_STATUSES = {429, 500, 502, 503, 504}


async def fetch_with_httpx(
    url: str,
    timeout_seconds: int,
    retries: int,
    proxy: str | None,
    user_agent: str,
    max_bytes: int,
    allow_insecure_ssl: bool | None = None,
) -> tuple[int, str, bytes, str | None]:
    """
    Returns: (status_code, final_url, content_bytes, content_type)
    """
    req_headers = {"User-Agent": user_agent}
    timeout = httpx.Timeout(timeout_seconds)

    # Use persistent client unless insecure SSL is requested (rare test override)
    insecure = allow_insecure_ssl if allow_insecure_ssl is not None else settings.allow_insecure_ssl
    if insecure:
        # Short-lived client – only created for the insecure path
        async with _make_client(verify=False, proxy=proxy) as client:
            return await _do_fetch(client, url, req_headers, timeout, max_bytes, retries)

    client = get_http_client()
    # Proxy requests need a dedicated client (proxy is set at client level in httpx)
    if proxy:
        async with _make_client(_SSL_CONTEXT, proxy=proxy) as pclient:
            return await _do_fetch(pclient, url, req_headers, timeout, max_bytes, retries)

    return await _do_fetch(client, url, req_headers, timeout, max_bytes, retries)


async def _do_fetch(
    client: httpx.AsyncClient,
    url: str,
    extra_headers: dict,
    timeout: httpx.Timeout,
    max_bytes: int,
    retries: int,
) -> tuple[int, str, bytes, str | None]:
    last_exc: Exception | None = None
    last_status: int = 0
    for attempt in range(retries + 1):
        try:
            # Stream to enforce max_bytes
            async with client.stream("GET", url, headers=extra_headers, timeout=timeout) as resp:
                status = resp.status_code
                final_url = str(resp.url)
                ctype = resp.headers.get("content-type")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            break
                result = status, final_url, bytes(buf[:max_bytes]), ctype

            # Retry on transient server errors / rate-limit if retries remain
            if status in _RETRY_STATUSES and attempt < retries:
                last_status = status
                delay = min(2 ** attempt, 8)
                # Honour Retry-After header when present (429/503)
                retry_after = resp.headers.get("retry-after")
                if retry_after:
                    try:
                        delay = min(int(retry_after), 30)
                    except ValueError:
                        pass
                await asyncio.sleep(delay)
                continue

            return result
        except Exception as e:
            last_exc = e
            # Exponential backoff: skip sleep after the last attempt
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 5))
    # Retries exhausted
    if last_exc:
        raise last_exc
    if last_status:
        # Return the last error response rather than raising
        return last_status, url, b"", None
    raise RuntimeError("Unknown fetch error")
