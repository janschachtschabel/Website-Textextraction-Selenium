from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import tempfile
import threading
import time
from contextlib import asynccontextmanager

import diskcache
import httpx
from fastapi import Body, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from selenium.common.exceptions import WebDriverException
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .anonymizer import anonymize as presidio_anonymize
from .anonymizer import init_anonymizer
from .config import settings
from .converter import bytes_to_markdown
from .http_fetcher import close_http_client, fetch_with_httpx, init_http_client
from .js_fetcher import _initialize_pool, cleanup_drivers, fetch_with_playwright, get_pool_stats
from .logging_setup import setup_logging
from .metrics import close_metrics, get_window_stats, init_metrics, record_request
from .preflight import preflight as preflight_analyze
from .rate_limiter import acquire as rate_limit_acquire
from .rate_limiter import init_rate_limiters
from .schemas import (
    AnonymizationResult,
    BatchCrawlItemResult,
    BatchCrawlRequest,
    BatchCrawlResponse,
    CrawlRequest,
    CrawlResponse,
    LinkInfo,
)
from .utils import detect_error_page, extract_links_detailed_from_html, is_ssrf_url, normalize_proxy, pick_user_agent

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_http_bearer = HTTPBearer(auto_error=False)


def _check_auth(credentials: HTTPAuthorizationCredentials | None = Security(_http_bearer)) -> None:
    """Validate Bearer token when API_KEY is configured. No-op when auth is disabled."""
    if not settings.api_key:
        return  # auth disabled
    if credentials is None or credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")


# ---------------------------------------------------------------------------
# Pool warm-up state (for /health)
# ---------------------------------------------------------------------------
_pools_warming: bool = True  # True until background warm-up finishes
_pools_warming_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Result cache (diskcache — SQLite-backed, process-safe across uvicorn workers)
# ---------------------------------------------------------------------------
_result_cache: diskcache.Cache | None = None


def _init_cache() -> None:
    """Initialise the shared disk cache.  Called once per worker in lifespan startup."""
    global _result_cache
    if settings.result_cache_ttl <= 0:
        logger.debug("Result cache disabled (RESULT_CACHE_TTL=0)")
        return
    cache_dir = settings.result_cache_dir or os.path.join(
        tempfile.gettempdir(), "volltextextraktion_cache"
    )
    size_limit = settings.result_cache_max_size * 1024 * 1024  # MB → bytes
    _result_cache = diskcache.Cache(
        directory=cache_dir,
        size_limit=size_limit or None,
        # --- Multi-worker SQLite tuning ---
        # WAL mode: concurrent lock-free reads across all workers; only writes serialize.
        sqlite_journal_mode="wal",
        # 64 MB memory-mapped I/O: reads go directly from OS page cache — no SQLite lock needed.
        sqlite_mmap_size=64 * 1024 * 1024,
        # synchronous=OFF: skip fsync on every write. Cache is ephemeral — a crash loses at
        # most the last few cached responses, not application data.
        sqlite_synchronous=0,
        # Disable diskcache’s internal hit/miss counter table: each get()/set() would otherwise
        # do 1–2 extra SQL writes to the statistics table, serialising workers needlessly.
        statistics=0,
        # WAL auto-checkpoint threshold: flush WAL to main DB after 1000 pages (~4 MB).
        # Keeps read performance stable without manual PRAGMA wal_checkpoint calls.
        sqlite_wal_autocheckpoint=1000,
    )
    logger.info(
        f"[PID {os.getpid()}] Shared cache ready: {cache_dir} "
        f"(limit={settings.result_cache_max_size} MB, TTL={settings.result_cache_ttl}s, WAL+mmap)"
    )


def _close_cache() -> None:
    if _result_cache is not None:
        _result_cache.close()


def _get_cache() -> diskcache.Cache | None:
    """Return the result cache, or None when caching is disabled (TTL=0)."""
    return _result_cache if settings.result_cache_ttl > 0 else None


def _make_cache_key(req: CrawlRequest) -> str:
    """Deterministic cache key based on request params that affect raw content."""
    parts = [
        str(req.url),
        req.mode,
        req.js_strategy,
        str(req.html_converter),
        str(req.trafilatura_clean_markdown),
        str(req.max_bytes),
        str(req.allow_insecure_ssl),
        str(req.extract_links),
        str(req.screenshot),
        str(req.anonymize),
        req.anonymize_language,
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Enhanced request capacity management (simplified and deadlock-safe)
_concurrent_requests = 0
_waiting_count = 0  # number of requests waiting to acquire capacity
_max_concurrent = settings.selenium_max_pool_size  # Use max pool size for better capacity
# Semaphore and lock are created lazily to avoid DeprecationWarning (Python 3.10+)
# when instantiating asyncio primitives outside a running event loop.
_request_semaphore: asyncio.Semaphore | None = None
_request_lock: asyncio.Lock | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _request_semaphore
    if _request_semaphore is None:
        _request_semaphore = asyncio.Semaphore(_max_concurrent)
    return _request_semaphore


def _get_lock() -> asyncio.Lock:
    global _request_lock
    if _request_lock is None:
        _request_lock = asyncio.Lock()
    return _request_lock


class SmartCapacityMiddleware(BaseHTTPMiddleware):
    """Capacity management using a semaphore and bounded waiting with timeout.

    This design avoids awaiting while holding the internal lock to prevent deadlocks.
    """

    async def dispatch(self, request: Request, call_next):
        global _concurrent_requests, _waiting_count

        # Only apply limits to crawl endpoints
        if request.url.path not in ("/crawl", "/crawl/batch"):
            return await call_next(request)

        # Check if we can enter immediately or must wait. We never await while holding the lock.
        # Enforce a bounded waiting room using _waiting_count against max_queue_size.
        lock = _get_lock()
        semaphore = _get_semaphore()
        async with lock:
            can_enter_now = _concurrent_requests < _max_concurrent
            if not can_enter_now:
                if _waiting_count >= settings.max_queue_size:
                    logger.warning(
                        f"Request rejected: waiting room full ({_waiting_count}/{settings.max_queue_size})"
                    )
                    return JSONResponse(
                        content={"detail": "Server overloaded. Queue is full. Please retry later."},
                        status_code=503,
                    )
                _waiting_count += 1

        acquired = False
        try:
            # Try to acquire capacity with a timeout (queueing behavior)
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=settings.queue_timeout_seconds)
                acquired = True
            except TimeoutError:
                async with lock:
                    if _waiting_count > 0:
                        _waiting_count -= 1
                return JSONResponse(
                    content={"detail": "Request timed out in queue"}, status_code=504
                )

            # We have capacity, update counters
            async with lock:
                if _waiting_count > 0:
                    _waiting_count -= 1
                _concurrent_requests += 1

            # Process the request
            try:
                response = await call_next(request)
                return response
            except Exception as e:
                logger.error(f"Request processing error: {e}")
                return JSONResponse(
                    content={"detail": f"Request processing failed: {e!s}"}, status_code=502
                )
            finally:
                async with lock:
                    _concurrent_requests -= 1
        finally:
            if acquired:
                semaphore.release()


# Removed request object queuing helpers; semaphore-based waiting is used instead.


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up the Selenium driver pools in a background thread so that the
    # first real request does not incur the 30-60 s cold-start penalty.
    def _warm_pools():
        global _pools_warming
        pid = os.getpid()
        logger.info(f"[PID {pid}] Selenium pool warm-up started")
        for key in ("normal", "eager"):
            try:
                _initialize_pool(key)
                logger.debug(f"[PID {pid}] Pool '{key}' ready")
            except Exception as exc:
                logger.warning(f"[PID {pid}] Pool warm-up failed for {key}: {exc}")
        with _pools_warming_lock:
            _pools_warming = False
        logger.info(f"[PID {pid}] Selenium driver pools ready")

    setup_logging(level=settings.log_level, json_logs=settings.log_json)
    await init_http_client()
    init_rate_limiters()
    _init_cache()
    init_metrics()
    threading.Thread(target=_warm_pools, daemon=True, name="pool-warmup").start()
    threading.Thread(
        target=init_anonymizer,
        kwargs={"de_model": settings.presidio_de_model, "en_model": settings.presidio_en_model},
        daemon=True,
        name="presidio-init",
    ).start()
    yield
    cleanup_drivers()
    await close_http_client()
    _close_cache()
    close_metrics()


app = FastAPI(title="Volltextextraktion Selenium MD", version="0.1.0", lifespan=lifespan)
app.add_middleware(SmartCapacityMiddleware)


@app.get("/", include_in_schema=False)
async def root():
    """Redirect to interactive API documentation."""
    return RedirectResponse(url="/docs")


@app.get("/stats")
async def get_stats(_: None = Security(_check_auth)):
    """Returns operational metrics for the API.

    ## Fields

    ### Capacity (current)
    - **`concurrent_requests`**: Requests being processed right now
    - **`max_concurrent`**: Maximum simultaneous requests (= Selenium pool size)
    - **`queue_size`**: Queued requests (backlog)
    - **`max_queue_size`**: Maximum queue length
    - **`capacity_utilization`**: Overview of processing/queue/total as strings

    ### Selenium Pools
    - **`selenium_pools`**: State of the browser driver pools per strategy

    ### Rolling Window (last hour, all workers aggregated)
    All metrics are available under `last_hour` as a sliding 1-hour window:
    - **`requests_total`** / **`requests_success`** / **`requests_error`**: counters
    - **`cache_hits`** / **`cache_hit_rate_pct`** / **`cache_entries_current`**: cache efficiency
    - **`latency_seconds`**: p50/p95/avg/n per mode (fast/js/auto)
    - **`errors_by_type`**: error count per type (timeout, http_5xx, …)
    - **`throughput_per_minute`**: requests/minute for the last 60 minutes (array of 60 values)
    """
    pool_stats = get_pool_stats()
    global _concurrent_requests

    async with _get_lock():
        queue_size = _waiting_count
        concurrent = _concurrent_requests

    cache_obj = _get_cache()
    window = get_window_stats(cache_obj)

    return {
        "concurrent_requests": concurrent,
        "max_concurrent": _max_concurrent,
        "queue_size": queue_size,
        "max_queue_size": settings.max_queue_size,
        "selenium_pools": pool_stats,
        "capacity_utilization": {
            "processing": f"{concurrent}/{_max_concurrent}",
            "queue": f"{queue_size}/{settings.max_queue_size}",
            "total_capacity": f"{concurrent + queue_size}/{_max_concurrent + settings.max_queue_size}"
        },
        "last_hour": window,
    }


@app.post(
    "/crawl",
    response_model=CrawlResponse,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "standard": {
                            "summary": "Default values",
                            "value": {
                                "url": "https://example.com",
                                "mode": "auto",
                                "js_strategy": "speed",
                                "html_converter": "trafilatura",
                                "trafilatura_clean_markdown": True,
                                "media_conversion_policy": "skip",
                                "allow_insecure_ssl": True,
                                "extract_links": False,
                                "screenshot": True,
                                "anonymize": False,
                                "anonymize_language": "de",
                                "crawl_rate_limit_rps": None,
                                "retries": 2,
                                "timeout_ms": 30000,
                                "max_bytes": 1048576,
                            }
                        }
                    }
                }
            }
        }
    },
)
async def crawl(
    req: CrawlRequest = Body(
        ...,
        openapi_examples={
            "standard": {
                "summary": "Default values",
                "description": "Example request body with fields in recommended order",
                "value": {
                    "url": "https://example.com",
                    "mode": "auto",
                    "js_strategy": "speed",
                    "html_converter": "trafilatura",
                    "trafilatura_clean_markdown": True,
                    "media_conversion_policy": "skip",
                    "allow_insecure_ssl": True,
                    "extract_links": False,
                    "screenshot": True,
                    "anonymize": False,
                    "anonymize_language": "de",
                    "crawl_rate_limit_rps": None,
                    "retries": 2,
                    "timeout_ms": 30000,
                    "max_bytes": 1048576,
                },
            }
        }
    ),
    _: None = Security(_check_auth),
):
    """
    Crawls a web page and automatically converts it to structured Markdown.

    ## 🚀 Three Modes:

    ### `auto` - Automatic Selection
    - **How it works**: Lightweight pre-flight analysis (httpx + HTML parsing)
    - **Decision logic**:
      - PDF/RSS/YouTube → served directly without Selenium
      - Sufficient HTML text → served directly without Selenium
      - JS/SPAs/CMP detected → Selenium is started (default strategy `speed`, configurable via `js_strategy`)

    ### `fast` - Fast HTTP Mode
    - **Ideal for**: Static websites, document downloads, APIs
    - **Supports**: HTML, PDF, DOCX, PPTX, XLSX, images, text files
    - **Features**: HTTP/2, automatic redirects, cookie persistence, proxy support
    - **Performance**: Very fast (1–5 s), resource-efficient
    - **Limitations**: No JavaScript rendering, no dynamic content

    ### `js` - Browser Rendering Mode
    - **Ideal for**: Single-page applications, JavaScript-heavy pages, modern web apps
    - **Engine**: Selenium Chrome WebDriver with stealth features
    - **Features**: Full browser behaviour, cookie banner dismissal, DOM wait
    - **Performance**: Slower (5–30 s), higher resource usage
    - **Advantages**: Renders JavaScript, waits for dynamic content

    ## JS Strategy (optional)
    Controls wait/stability heuristics in JS mode via `js_strategy` (default: `speed`):

    - `speed` (default):
      - Aggressive wait reduction with polling and early-exit
      - Very short SPA/loader/progressive caps; best-effort; suited for fast scans and batch runs
    - `accuracy`:
      - Maximum quality/robustness with slightly more conservative caps

    ## 🔧 Per-Request Schema Overrides
    Server `.env` defaults can optionally be overridden per request:
    - `html_converter`: "trafilatura" | "markitdown" | "bs4" (default from .env)
    - `trafilatura_clean_markdown`: true | false | null (null = .env default)
    - `media_conversion_policy`: "skip" | "metadata" | "full" | "none" (default: "skip")
      - Note: "none" produces no Markdown output for media files
    - `allow_insecure_ssl`: true | false | null (null = .env default)

    ## 📄 Supported Formats:
    - **Web**: HTML, XHTML, XML, RSS/Atom feeds
    - **Office**: DOCX, PPTX, XLSX, ODT, ODS, ODP
    - **PDF**: All PDF versions with text extraction
    - **Images**: JPG, PNG, GIF, WebP, SVG (with OCR support)
    - **Text**: TXT, CSV, JSON, Markdown, RTF
    - **Code**: Syntax highlighting for all common programming languages

    ## 📷 Screenshot (Optional):

    Enable with `screenshot: true`. Captures a **viewport screenshot** (1920×1080 px)
    of the fully rendered browser page as PNG.

    - Result is available in the `screenshot_base64` field as a Base64-encoded PNG
    - Captured **after** cookie banner dismissal and DOM stabilisation
    - **Only available** when Selenium is active:
      - `mode=js`: always available
      - `mode=auto`: available when the JS path was chosen
      - `mode=fast` or `auto`+HTTP_ONLY: `screenshot_base64` is `null`
    - No performance overhead when `screenshot=false` (default)

    ## 🔒 PII Anonymisation (Optional):

    Enable with `anonymize: true`. Runs **locally** via [Microsoft Presidio](https://microsoft.github.io/presidio/) — no external API call, no cost.

    - Detected entities are **replaced** directly in the `markdown` field, e.g.:
      - `John Smith` → `<PERSON>`
      - `john@example.com` → `<EMAIL_ADDRESS>`
      - `+1-555-0100` → `<PHONE_NUMBER>`
      - `New York` → `<LOCATION>`
    - Supported languages: `de` (German, default) and `en` (English) via `anonymize_language`
    - The `anonymization` field in the response contains metadata: detected PII types and count
    - **Note**: Models are loaded in the background at server start (~30–60 s).
      Until then `anonymization.warning` returns a notice.

    ## 🚦 Rate Limiting (Optional):

    Controls the request rate to the **target domain** of this request:

    - `crawl_rate_limit_rps: null` (default): server default from `DEFAULT_DOMAIN_RATE_LIMIT_RPS` applies
    - `crawl_rate_limit_rps: 0.0`: **disable** per-domain limit for this request
    - `crawl_rate_limit_rps: 1.0`: max 1 request/s to this domain

    A separate **global limit** (`GLOBAL_RATE_LIMIT_RPS`) is configurable server-side only via `.env`
    and cannot be overridden per request. Both limits apply independently.

    ## 🔗 Link Extraction:
    Automatically categorises all detected links:
    - **content**: Articles, resources, main content
    - **nav**: Navigation, menus, breadcrumbs
    - **social**: Social media, sharing buttons
    - **auth**: Login, registration, account areas
    - **legal**: Imprint, privacy policy, terms
    - **search**: Search functions, filters
    - **contact**: Contact information, support
    - **download**: Download links, attachments
    - **anchor**: Internal anchor links (#section)
    - **other**: Miscellaneous links

    ## ⚡ Performance & Security:
    - **Timeout control**: Configurable time limits (1 s – 10 min)
    - **Retry mechanism**: Automatic retry on failure
    - **Size limits**: Protection against oversized downloads
    - **Proxy support**: HTTP/HTTPS/SOCKS with authentication
    - **Anti-bot evasion**: Stealth features, user-agent rotation
    - **Resource pool**: Efficient browser instance reuse

    ## 📊 Response Format:
    - **`markdown`**: Extracted content as Markdown — already anonymised when `anonymize=true`
    - **`markdown_length`**: Character count of the Markdown field
    - **`request_mode`**: Actually used mode (relevant for `auto`)
    - **`final_url`**: URL after all redirects
    - **`status_code`**: HTTP status code of the target page
    - **`error_page_detected`**: True when 404, 403, 503 etc. detected in page content
    - **`cached`**: True when result is served from the shared result cache (False with `force_refresh=true`)
    - **`links`**: Categorised link list (only with `extract_links=true`)
    - **`screenshot_base64`**: Viewport PNG as Base64 (only with `screenshot=true` and Selenium active)
    - **`anonymization`**: PII metadata (only with `anonymize=true`)
    - **`elapsed_ms`**: Total request duration in milliseconds

    **Duration**: fast mode 1–5 s, js mode 5–30 s, screenshot +0–1 s, anonymisation +0.1–2 s
    """
    # SSRF protection: reject requests to private/loopback addresses
    if settings.ssrf_protection and is_ssrf_url(str(req.url)):
        raise HTTPException(
            status_code=400,
            detail="SSRF protection: requests to private/internal addresses are not allowed.",
        )

    ua = pick_user_agent(settings.default_user_agent)
    timeout_ms = req.timeout_ms or (settings.default_timeout_seconds * 1000)
    timeout_s = max(1, int((timeout_ms + 999) // 1000))
    retries = req.retries if req.retries is not None else settings.default_retries
    max_bytes = req.max_bytes or settings.default_max_bytes
    proxy_norm = normalize_proxy(req.proxy)

    # Cache lookup (skip when proxy is set or force_refresh requested)
    cache = _get_cache()
    cache_key = _make_cache_key(req) if (cache is not None and not proxy_norm) else None
    if cache_key and not req.force_refresh:
        cached_resp = cache.get(cache_key)  # diskcache is process- and thread-safe
        if cached_resp is not None:
            logger.debug(f"Cache hit for {req.url}")
            record_request(req.mode, 0.0, success=True, cached=True)
            return cached_resp.model_copy(update={"cached": True})
    elif cache_key and req.force_refresh:
        logger.debug(f"Cache bypass (force_refresh) for {req.url}")

    # Rate limiting: wait for global + per-domain slots before fetching
    await rate_limit_acquire(str(req.url), req.crawl_rate_limit_rps)

    t0 = time.perf_counter()
    pf_html_text: str | None = None  # raw HTML from preflight (for error detection)
    screenshot_png: bytes | None = None  # set when Selenium takes a screenshot
    pf_strategy: str | None = None      # preflight strategy chosen (auto mode only)
    try:
        if req.mode == "fast":
            status, final_url, data, ctype = await fetch_with_httpx(
                url=str(req.url),
                timeout_seconds=timeout_s,
                retries=retries,
                proxy=proxy_norm,
                user_agent=ua,
                max_bytes=max_bytes,
                allow_insecure_ssl=req.allow_insecure_ssl,
            )
        elif req.mode == "auto":
            # Lightweight preflight to pick best path quickly
            pf = await preflight_analyze(
                str(req.url),
                timeout_seconds=min(timeout_s, 12),
                user_agent=ua,
                allow_insecure_ssl=req.allow_insecure_ssl,
            )
            strat = pf.get("strategy")
            pf_strategy = strat  # remember for post-conversion fallback
            pf_html_text = pf.get("html_text")  # preserve for raw-HTML error detection
            # Direct return cases without Selenium
            if strat in {"PDF", "RSS", "HTTP_ONLY", "YOUTUBE"}:
                status = pf.get("status", 200)
                final_url = pf.get("final_url", str(req.url))
                data = pf.get("content_bytes") or (pf.get("html_text") or "").encode("utf-8")
                ctype = pf.get("content_type") or ("text/html; charset=utf-8" if pf.get("html_text") else None)
            elif strat == "BLOCKED":
                status = pf.get("status", 200)
                final_url = pf.get("final_url", str(req.url))
                data = pf.get("content_bytes") or b""
                ctype = pf.get("content_type")
                pf_features = pf.get("features", {})
                # Always try Selenium — plain HTTP blocks (403/429) and many
                # content-based patterns (embedded reCAPTCHA widget, CDN footer)
                # are routinely bypassed by headless Chrome with stealth mode.
                # Only give up when Selenium itself throws an exception.
                block_reason = "HTTP " + str(pf_features["blocked_status"]) if "blocked_status" in pf_features else "content pattern"
                logger.info(f"auto-mode BLOCKED ({block_reason}) for {req.url} – retrying with Selenium (stealth)")
                try:
                    js_strategy = req.js_strategy or settings.default_js_strategy
                    js_auto_wait = settings.default_js_auto_wait
                    wait_selectors = [
                        "article", "main", "#content", "#main-content", "[role=main]"
                    ] if js_auto_wait else None
                    wait_ms = 2000 if js_auto_wait else None
                    status, final_url, data, ctype, screenshot_png = await fetch_with_playwright(
                        url=str(req.url),
                        timeout_seconds=timeout_s,
                        retries=retries,
                        proxy=proxy_norm,
                        user_agent=ua,
                        max_bytes=max_bytes,
                        headless=True,
                        stealth=True,
                        wait_for_selectors=wait_selectors,
                        wait_for_ms=wait_ms,
                        js_strategy=js_strategy,
                        allow_insecure_ssl=req.allow_insecure_ssl,
                        take_screenshot=req.screenshot,
                    )
                    # Continue to normal markdown conversion below
                except Exception as _blocked_e:
                    logger.warning(f"auto-mode Selenium fallback failed for {req.url}: {_blocked_e}")
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)
                    return CrawlResponse(
                        request_mode=req.mode,
                        requested_url=str(req.url),
                        final_url=final_url,
                        status_code=status,
                        redirected=(final_url.rstrip("/") != str(req.url).rstrip("/")),
                        content_type=ctype,
                        markdown="",
                        markdown_length=0,
                        word_count=0,
                        error_page_detected=True,
                        links=None,
                        screenshot_base64=None,
                        elapsed_ms=elapsed_ms,
                    )
            else:
                # JS paths: JS_LIGHT / JS_LIGHT_CONSENT / HTTP_THEN_JS
                if strat == "HTTP_THEN_JS" and (pf.get("features", {}).get("text_len", 0) >= 700):
                    # Good enough without JS
                    status = pf.get("status", 200)
                    final_url = pf.get("final_url", str(req.url))
                    data = pf.get("content_bytes") or (pf.get("html_text") or "").encode("utf-8")
                    ctype = pf.get("content_type") or "text/html; charset=utf-8"
                else:
                    # Run Selenium for JS_LIGHT and friends; respect provided js_strategy
                    js_strategy = req.js_strategy or "speed"
                    js_auto_wait = settings.default_js_auto_wait
                    wait_selectors = [
                        "article", "main", "#content", "#main-content", "[role=main]"
                    ] if js_auto_wait else None
                    wait_ms = 1500 if js_auto_wait else None
                    status, final_url, data, ctype, screenshot_png = await fetch_with_playwright(
                        url=str(req.url),
                        timeout_seconds=timeout_s,
                        retries=retries,
                        proxy=proxy_norm,
                        user_agent=ua,
                        max_bytes=max_bytes,
                        headless=True,
                        stealth=True,
                        wait_for_selectors=wait_selectors,
                        wait_for_ms=wait_ms,
                        js_strategy=js_strategy,
                        allow_insecure_ssl=req.allow_insecure_ssl,
                        take_screenshot=req.screenshot,
                    )
        else:
            # JS defaults: headless+stealth always on; optional auto waits from config
            js_auto_wait = settings.default_js_auto_wait
            wait_selectors = ["article", "main", "#content", "#main-content", "[role=main]"] if js_auto_wait else None
            wait_ms = 2000 if js_auto_wait else None
            # Determine JS strategy (accuracy|speed)
            js_strategy = req.js_strategy or settings.default_js_strategy
            status, final_url, data, ctype, screenshot_png = await fetch_with_playwright(
                url=str(req.url),
                timeout_seconds=timeout_s,
                retries=retries,
                proxy=proxy_norm,
                user_agent=ua,
                max_bytes=max_bytes,
                headless=True,
                stealth=True,
                wait_for_selectors=wait_selectors,
                wait_for_ms=wait_ms,
                js_strategy=js_strategy,
                allow_insecure_ssl=req.allow_insecure_ssl,
                take_screenshot=req.screenshot,
            )
    except Exception as e:
        msg = str(e) or repr(e)
        logger.error(f"Fetch error for {req.url}: {type(e).__name__}: {msg}")
        # Map specific error types to more precise status codes
        status_code = 502
        low = msg.lower()
        if isinstance(e, httpx.ReadTimeout | httpx.ConnectTimeout) or "timeout" in low:
            status_code = 504  # Gateway Timeout / upstream timeout
        elif isinstance(e, WebDriverException) and ("timed out receiving message from renderer" in low):
            status_code = 504
        elif isinstance(e, httpx.ConnectError):
            status_code = 502  # Bad Gateway / upstream connect error
        elapsed_err = time.perf_counter() - t0
        record_request(req.mode, elapsed_err, success=False, cached=False, error_type=f"http_{status_code}")
        raise HTTPException(status_code=status_code, detail=f"Fetch error: {type(e).__name__}: {msg}") from e

    # Convert to markdown with error handling
    try:
        markdown = await run_in_threadpool(
            bytes_to_markdown,
            data,
            content_type=ctype,
            url=str(req.url),
            html_converter=req.html_converter,
            trafilatura_clean_markdown=req.trafilatura_clean_markdown,
            media_conversion_policy=req.media_conversion_policy,
        )
    except Exception as e:
        logger.error(f"Markdown conversion failed for {req.url}: {e}")
        # Return a meaningful error response instead of crashing
        markdown = f"# Content Conversion Failed\n\nFailed to convert content from {req.url}\n\nError: {e!s}\n\nThis may be due to a corrupted file, unsupported format, or network issue."

    # auto-mode fallback: if HTTP path produced empty markdown, retry with Selenium
    if (
        req.mode == "auto"
        and pf_strategy in {"HTTP_ONLY", "HTTP_THEN_JS"}
        and not markdown.strip()
    ):
        logger.info(f"auto-mode JS fallback triggered for {req.url} (HTTP path returned empty markdown)")
        js_strategy = req.js_strategy or settings.default_js_strategy
        js_auto_wait = settings.default_js_auto_wait
        wait_selectors = ["article", "main", "#content", "#main-content", "[role=main]"] if js_auto_wait else None
        wait_ms = 1500 if js_auto_wait else None
        try:
            status, final_url, data, ctype, screenshot_png = await fetch_with_playwright(
                url=str(req.url),
                timeout_seconds=timeout_s,
                retries=retries,
                proxy=proxy_norm,
                user_agent=ua,
                max_bytes=max_bytes,
                headless=True,
                stealth=True,
                wait_for_selectors=wait_selectors,
                wait_for_ms=wait_ms,
                js_strategy=js_strategy,
                allow_insecure_ssl=req.allow_insecure_ssl,
                take_screenshot=req.screenshot,
            )
            markdown = await run_in_threadpool(
                bytes_to_markdown,
                data,
                content_type=ctype,
                url=str(req.url),
                html_converter=req.html_converter,
                trafilatura_clean_markdown=req.trafilatura_clean_markdown,
                media_conversion_policy=req.media_conversion_policy,
            )
        except Exception as e:
            logger.warning(f"auto-mode JS fallback failed for {req.url}: {e}")

    # Optional link extraction (only for HTML-like data)
    links = None
    if req.extract_links and (ctype or "").lower().startswith("text/html"):
        try:
            html_text = data.decode("utf-8", errors="ignore")
            details = extract_links_detailed_from_html(html_text, final_url)
            links = [LinkInfo(**d) for d in details]
        except Exception:
            links = None

    # Error-page detection: check raw HTML first (patterns may be stripped by converter),
    # then fall back to the converted markdown.
    err = detect_error_page(pf_html_text or "", status) if pf_html_text else False
    if not err:
        err = detect_error_page(markdown, status, check_thin=True)

    # Optional PII anonymization via Presidio (runs in threadpool — CPU-bound)
    anon_payload: AnonymizationResult | None = None
    if req.anonymize:
        anon_text, anon_meta = await run_in_threadpool(
            presidio_anonymize, markdown, req.anonymize_language
        )
        markdown = anon_text
        anon_payload = AnonymizationResult(
            entities_found=anon_meta.entities_found,
            entity_count=anon_meta.entity_count,
            warning=anon_meta.warning,
        )

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    screenshot_b64 = base64.b64encode(screenshot_png).decode() if screenshot_png else None

    _md = markdown or ""
    resp = CrawlResponse(
        request_mode=req.mode,
        requested_url=str(req.url),
        final_url=final_url,
        status_code=status,
        redirected=(final_url.rstrip('/') != str(req.url).rstrip('/')),
        content_type=ctype,
        markdown=_md,
        markdown_length=len(_md),
        word_count=len(_md.split()),
        error_page_detected=err,
        links=links,
        screenshot_base64=screenshot_b64,
        anonymization=anon_payload,
        elapsed_ms=elapsed_ms,
    )

    # Store in cache (only successful, non-error results)
    if cache_key and cache is not None and not err:
        cache.set(cache_key, resp, expire=settings.result_cache_ttl)

    record_request(req.mode, (time.perf_counter() - t0), success=True, cached=False)
    return resp


@app.get("/health")
async def health():
    """Check the health status of the API.

    Returns HTTP 200 as long as the process is running — including during warm-up.
    HTTP 503 when no Selenium driver is available (degraded).

    ## Fields
    - **`status`**: `ok` | `starting` (warm-up in progress) | `degraded` (no driver)
    - **`driver_pool_ready`**: True when at least one Chrome driver is ready
    - **`pools_warming`**: True during initial driver pool initialisation
    - **`selenium_pools`**: Detailed state per pool (available, busy, total)
    - **`cache`**: Size and configuration of the result cache
    """
    pool_stats = get_pool_stats()

    with _pools_warming_lock:
        warming = _pools_warming

    driver_ok = False
    try:
        from .js_fetcher import _driver_pools
        for pool_q in _driver_pools.values():
            if not pool_q.empty():
                driver_ok = True
                break
    except Exception:
        pass

    result_cache = _get_cache()
    cache_info = (
        {"size": len(result_cache), "max_size_mb": settings.result_cache_max_size, "ttl": settings.result_cache_ttl}
        if result_cache is not None
        else {"disabled": True}
    )

    if warming:
        http_status = 200  # alive but still initialising — don't fail Kubernetes readiness
        app_status = "starting"
    elif driver_ok:
        http_status = 200
        app_status = "ok"
    else:
        http_status = 503
        app_status = "degraded"

    return JSONResponse(
        content={
            "status": app_status,
            "driver_pool_ready": driver_ok,
            "pools_warming": warming,
            "selenium_pools": pool_stats,
            "cache": cache_info,
        },
        status_code=http_status,
    )


@app.post("/crawl/batch", response_model=BatchCrawlResponse)
async def crawl_batch(
    req: BatchCrawlRequest = Body(...),
    _: None = Security(_check_auth),
):
    """Crawls up to 50 URLs in parallel with shared settings.

    All parameters from `/crawl` are also available here and apply to every URL.
    Errors for individual URLs are recorded per entry — the batch always runs to completion.

    ## Notes
    - **`max_concurrency`** controls parallelism *within* the batch (default: 3)
    - Global server capacity limits still apply (queue limits)
    - Timeout `timeout_ms` applies **per URL**, not to the entire batch
    - With `screenshot=true` a viewport screenshot is captured per URL
    - With `anonymize=true` each URL is anonymised individually
    - **`crawl_rate_limit_rps`** applies per target domain across all batch URLs;
      `null` = server default, `0.0` = no limit, `> 0` = max requests/s per domain

    ## Response
    - **`total`** / **`succeeded`** / **`failed`**: summary counts
    - **`results`**: individual results in input order (including error detail on failure)
    - **`elapsed_ms`**: total duration of the batch request
    """
    t0 = time.perf_counter()
    sem = asyncio.Semaphore(req.max_concurrency)

    async def _crawl_one(url: str) -> BatchCrawlItemResult:
        async with sem:
            try:
                single = CrawlRequest(
                    url=url,  # type: ignore[arg-type]
                    mode=req.mode,
                    js_strategy=req.js_strategy,
                    html_converter=req.html_converter,
                    trafilatura_clean_markdown=req.trafilatura_clean_markdown,
                    media_conversion_policy=req.media_conversion_policy,
                    allow_insecure_ssl=req.allow_insecure_ssl,
                    timeout_ms=req.timeout_ms,
                    retries=req.retries,
                    proxy=req.proxy,
                    max_bytes=req.max_bytes,
                    extract_links=req.extract_links,
                    screenshot=req.screenshot,
                    anonymize=req.anonymize,
                    anonymize_language=req.anonymize_language,
                    crawl_rate_limit_rps=req.crawl_rate_limit_rps,
                    force_refresh=req.force_refresh,
                )
                result = await crawl(single)
                return BatchCrawlItemResult(url=url, success=True, result=result)
            except HTTPException as exc:
                return BatchCrawlItemResult(url=url, success=False, error=exc.detail)
            except Exception as exc:
                return BatchCrawlItemResult(url=url, success=False, error=str(exc) or repr(exc))

    tasks = [_crawl_one(str(u)) for u in req.urls]
    results: list[BatchCrawlItemResult] = list(await asyncio.gather(*tasks))

    succeeded = sum(1 for r in results if r.success)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return BatchCrawlResponse(
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=results,
        elapsed_ms=elapsed_ms,
    )
