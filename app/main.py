"""FastAPI boundary: authentication, schemas and endpoint adaptation."""

import math
import secrets
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Path, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger

from . import __version__
from .body_limit import BodySizeLimit
from .config import settings
from .inbound_limit import InboundLimit
from .logging_setup import setup_logging
from .prometheus import CONTENT_TYPE_LATEST, render
from .request_id import RequestId
from .resources import Resources
from .results import CrawlError
from .schemas import (
    BATCH_EXAMPLE,
    BATCH_FULL_EXAMPLE,
    CRAWL_EXAMPLE,
    CRAWL_FULL_EXAMPLE,
    BatchCrawlRequest,
    BatchCrawlResponse,
    CrawlRequest,
    CrawlResponse,
    JobAccepted,
    JobStatus,
    resolve_options,
)
from .selenium_driver import browser_status

DESCRIPTION = """Turns a web page into Markdown: fetched over HTTP, or rendered in Chrome
when the page needs JavaScript to show its content.

**Start here.** `POST /crawl` with a body of `{"url": "https://..."}`. Every other option
falls back to the operator's default, so the example on that endpoint is a complete
request, not a template to fill in.

**Choosing an engine.** `mode=fast` never starts a browser, `mode=js` always does, and
`mode=auto` starts one only when the plain HTML yields too little text. A screenshot needs
the browser.

**Several URLs.** `POST /crawl/batch` takes up to 50 and answers once all of them are done.
`POST /jobs` takes the same body, answers at once with a job id, and is polled at
`GET /jobs/{job_id}` - use it when a batch outlives the client's patience.

**Authentication.** When the operator configures an API key, every endpoint except `/` and
`/health` requires `Authorization: Bearer <key>`, and this page is not published at all.
"""


def _choices(minimal, complete, what):
    """Swagger offers these as a dropdown. The minimal body is the one to send; the
    complete one exists so every option is visible without leaving "Try it out"."""
    return {
        "minimal": {"summary": f"Minimal - {what}", "value": minimal},
        "complete": {"summary": "Every option named, at a working value", "value": complete},
    }


def create_app(config=settings, resources=None):
    @asynccontextmanager
    async def lifespan(application):
        setup_logging(config.log_level, config.log_json)
        browser = browser_status(config)
        for setting in ("chrome_binary", "chromedriver_path"):
            if browser[f"{setting.removesuffix('_path')}_exists"] is False:
                logger.warning(
                    "{} is set to {}, which does not exist; browser jobs will fail", setting.upper(), browser[setting]
                )
        async with resources or Resources(config) as active:
            application.state.resources = active
            yield

    # A key-protected deployment does not advertise its request surface.
    application = FastAPI(
        title="Website Text Extraction — Selenium",
        version=__version__,
        summary="Web pages as Markdown, with an optional browser for JavaScript sites",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None if config.api_key else "/docs",
        redoc_url=None if config.api_key else "/redoc",
        openapi_url=None if config.api_key else "/openapi.json",
    )
    application.add_middleware(BodySizeLimit, max_bytes=config.max_request_bytes)
    application.add_middleware(RequestId)  # added last, so it runs first and tags every answer
    bearer = HTTPBearer(auto_error=False)
    limit = (
        InboundLimit(config.inbound_rate_limit_rps, config.inbound_rate_limit_burst)
        if config.inbound_rate_limit_rps
        else None
    )

    def admit(urls: int):
        # Runs after authentication, so unauthenticated traffic cannot drain the bucket.
        wait = limit.take(urls) if limit else 0
        if wait:
            raise HTTPException(429, "Too many crawl requests", headers={"Retry-After": str(math.ceil(wait))})

    def check_auth(credentials: HTTPAuthorizationCredentials | None = Security(bearer)):
        if config.api_key and (
            credentials is None or not secrets.compare_digest(credentials.credentials.encode(), config.api_key.encode())
        ):
            raise HTTPException(401, "Invalid or missing API token", headers={"WWW-Authenticate": "Bearer"})

    @application.exception_handler(CrawlError)
    async def crawl_error_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @application.get("/", summary="Service banner")
    async def root():
        """Name, version and the path to this documentation. Public, so a probe can identify
        the service without holding a key."""
        return {"service": "Website Text Extraction", "version": __version__, "docs": application.docs_url}

    @application.get("/health", summary="Liveness, capacity and browser presence")
    async def health():
        """Public. Answers 200 while the service accepts work and 503 once it is stopping, and
        reports the capacity in use, the state of both worker pools and whether the configured
        Chrome and ChromeDriver exist. With an API key configured the paths themselves are
        withheld and only their presence is reported."""
        active = application.state.resources
        browser = browser_status(config)
        if config.api_key:
            # The endpoint stays public: report whether the browser is there, not where.
            browser = {name: value for name, value in browser.items() if name.endswith("_exists")}
        return JSONResponse(
            {
                "status": "ok" if active.ready else "stopping",
                "capacity": active.service.capacity.stats(),
                "selenium": active.browser_pool.stats(),
                "conversion": active.conversion_pool.stats(),
                "browser": browser,
            },
            status_code=200 if active.ready else 503,
        )

    @application.get("/stats", dependencies=[Security(check_auth)], summary="Counters of the last hour")
    async def stats():
        """Requests, errors, cache hits, coalesced answers and latency percentiles over a rolling
        60 minutes, with the throughput per minute, the entries in the result cache and the
        capacity in use."""
        active = application.state.resources
        count = await active.io(len, active.cache)
        result = await active.metrics.stats(count)
        result["capacity"] = active.service.capacity.stats()
        return result

    @application.get("/metrics", dependencies=[Security(check_auth)], summary="The same counters for Prometheus")
    async def metrics():
        """The counters of /stats in the Prometheus text format, with gauges for readiness, active
        and waiting URLs, cache entries and worker processes. The gauges describe the worker
        process that answers the scrape, not the deployment as a whole."""
        active = application.state.resources
        capacity = active.service.capacity.stats()
        pools = {"browser": active.browser_pool.stats(), "conversion": active.conversion_pool.stats()}
        gauges = {
            "extraction_ready": ("1 while the service accepts work", int(active.ready)),
            "extraction_active_requests": ("URLs being processed", capacity["active"]),
            "extraction_waiting_requests": ("URLs waiting for capacity", capacity["waiting"]),
            "extraction_cache_entries": ("Entries in the result cache", await active.io(len, active.cache)),
            "extraction_pool_workers": (
                "Worker processes per pool: limit, started and busy",
                {
                    (pool, state): stats[state]
                    for pool, stats in pools.items()
                    for state in ("limit", "started", "busy")
                },
            ),
        }
        return Response(render(await active.metrics.totals(), gauges), media_type=CONTENT_TYPE_LATEST)

    @application.post(
        "/crawl", response_model=CrawlResponse, dependencies=[Security(check_auth)], summary="Crawl one URL"
    )
    async def crawl(
        request: CrawlRequest = Body(openapi_examples=_choices(CRAWL_EXAMPLE, CRAWL_FULL_EXAMPLE, "just the URL")),
    ):
        """Fetch one URL and return it as Markdown.

        Only `url` is required; every other option falls back to the operator's default. The
        answer names the engine that ran and the converter that produced the text, and lists
        in `warnings` everything that degraded the result without failing it."""
        admit(1)
        return await application.state.resources.service.crawl(str(request.url), resolve_options(request, config))

    @application.post(
        "/crawl/batch",
        response_model=BatchCrawlResponse,
        dependencies=[Security(check_auth)],
        summary="Crawl up to 50 URLs and wait",
    )
    async def batch(
        request: BatchCrawlRequest = Body(
            openapi_examples=_choices(BATCH_EXAMPLE, BATCH_FULL_EXAMPLE, "just the URLs")
        ),
    ):
        """Crawl up to 50 URLs and answer once the last one is done.

        The connection is held open for the whole batch, which can be minutes.
        `max_concurrency` bounds how many run at once; the service-wide capacity may hold
        it lower. A URL that fails carries its own reason and does not fail the others.
        `POST /jobs` does the same crawling and answers immediately instead."""
        admit(len(request.urls))
        return await application.state.resources.service.crawl_batch(
            [str(url) for url in request.urls], resolve_options(request, config), request.max_concurrency
        )

    @application.post(
        "/jobs",
        status_code=202,
        response_model=JobAccepted,
        dependencies=[Security(check_auth)],
        summary="Submit up to 50 URLs as a background job",
    )
    async def submit_job(
        request: BatchCrawlRequest = Body(
            openapi_examples=_choices(BATCH_EXAMPLE, BATCH_FULL_EXAMPLE, "just the URLs")
        ),
    ):
        """Take the body of /crawl/batch, answer at once with a job id and crawl in the background.

        Same body and the same crawling as `/crawl/batch`; only the delivery differs. Use
        it when the connection would not survive the wait: proxies and tunnels commonly cut
        a request at about 125 seconds, while a batch may run for ten minutes. Poll
        `status_url`; the record stays readable for an hour after the job finishes."""
        admit(len(request.urls))
        job_id = await application.state.resources.jobs.submit(
            [str(url) for url in request.urls], resolve_options(request, config), request.max_concurrency
        )
        return JobAccepted(job_id=job_id, status="queued", status_url=f"/jobs/{job_id}")

    @application.get(
        "/jobs/{job_id}", response_model=JobStatus, dependencies=[Security(check_auth)], summary="Poll a background job"
    )
    async def job_status(job_id: str = Path(max_length=64)):
        """Report a submitted batch, and carry the result once it is done.

        Answers 404 when the id is unknown or its record has expired."""
        record = await application.state.resources.jobs.status(job_id)
        if record is None:
            raise HTTPException(404, "Unknown or expired job")
        return JobStatus(job_id=job_id, **{name: record[name] for name in JobStatus.model_fields if name != "job_id"})

    return application


app = create_app()
