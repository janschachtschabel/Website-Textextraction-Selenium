"""FastAPI boundary: authentication, schemas and endpoint adaptation."""

import math
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .body_limit import BodySizeLimit
from .config import settings
from .inbound_limit import InboundLimit
from .logging_setup import setup_logging
from .prometheus import CONTENT_TYPE_LATEST, render
from .resources import Resources
from .results import CrawlError
from .schemas import (
    BatchCrawlRequest,
    BatchCrawlResponse,
    CrawlRequest,
    CrawlResponse,
    JobAccepted,
    JobStatus,
    resolve_options,
)


def create_app(config=settings, resources=None):
    @asynccontextmanager
    async def lifespan(application):
        setup_logging(config.log_level, config.log_json)
        async with resources or Resources(config) as active:
            application.state.resources = active
            yield

    # A key-protected deployment does not advertise its request surface.
    application = FastAPI(
        title="Website Text Extraction — Selenium",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if config.api_key else "/docs",
        redoc_url=None if config.api_key else "/redoc",
        openapi_url=None if config.api_key else "/openapi.json",
    )
    application.add_middleware(BodySizeLimit, max_bytes=config.max_request_bytes)
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

    @application.get("/")
    async def root():
        return {"service": "Website Text Extraction", "version": __version__, "docs": application.docs_url}

    @application.get("/health")
    async def health():
        active = application.state.resources
        return JSONResponse(
            {
                "status": "ok" if active.ready else "stopping",
                "capacity": active.service.capacity.stats(),
                "selenium": active.browser_pool.stats(),
                "conversion": active.conversion_pool.stats(),
            },
            status_code=200 if active.ready else 503,
        )

    @application.get("/stats", dependencies=[Security(check_auth)])
    async def stats():
        active = application.state.resources
        count = await active.io(len, active.cache)
        result = await active.metrics.stats(count)
        result["capacity"] = active.service.capacity.stats()
        return result

    @application.get("/metrics", dependencies=[Security(check_auth)])
    async def metrics():
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

    @application.post("/crawl", response_model=CrawlResponse, dependencies=[Security(check_auth)])
    async def crawl(request: CrawlRequest):
        admit(1)
        return await application.state.resources.service.crawl(str(request.url), resolve_options(request, config))

    @application.post("/crawl/batch", response_model=BatchCrawlResponse, dependencies=[Security(check_auth)])
    async def batch(request: BatchCrawlRequest):
        admit(len(request.urls))
        return await application.state.resources.service.crawl_batch(
            [str(url) for url in request.urls], resolve_options(request, config), request.max_concurrency
        )

    @application.post("/jobs", status_code=202, response_model=JobAccepted, dependencies=[Security(check_auth)])
    async def submit_job(request: BatchCrawlRequest):
        admit(len(request.urls))
        job_id = await application.state.resources.jobs.submit(
            [str(url) for url in request.urls], resolve_options(request, config), request.max_concurrency
        )
        return JobAccepted(job_id=job_id, status="queued", status_url=f"/jobs/{job_id}")

    @application.get("/jobs/{job_id}", response_model=JobStatus, dependencies=[Security(check_auth)])
    async def job_status(job_id: str = Path(max_length=64)):
        record = await application.state.resources.jobs.status(job_id)
        if record is None:
            raise HTTPException(404, "Unknown or expired job")
        return JobStatus(job_id=job_id, **{name: record[name] for name in JobStatus.model_fields if name != "job_id"})

    return application


app = create_app()
