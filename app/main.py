"""FastAPI boundary: authentication, schemas and endpoint adaptation."""

import asyncio
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .config import settings
from .deadline import Deadline
from .logging_setup import setup_logging
from .resources import Resources
from .results import CrawlError
from .schemas import (
    BatchCrawlItemResult,
    BatchCrawlRequest,
    BatchCrawlResponse,
    CrawlRequest,
    CrawlResponse,
    resolve_options,
)


def _failure_reason(result: CrawlResponse) -> str:
    if result.extraction_status != "ok":
        return f"Extraction {result.extraction_status}"
    if result.truncated:
        return "Extraction truncated"
    if result.status_code is None:
        return "Upstream status unknown"
    return "Extraction incomplete"  # e.g. a rendered page that never settled; see warnings


def create_app(config=settings, resources=None):
    @asynccontextmanager
    async def lifespan(application):
        setup_logging(config.log_level, config.log_json)
        async with resources or Resources(config) as active:
            application.state.resources = active
            yield

    application = FastAPI(title="Website Text Extraction — Selenium", version=__version__, lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

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
        return {"service": "Website Text Extraction", "version": __version__, "docs": "/docs"}

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

    @application.post("/crawl", response_model=CrawlResponse, dependencies=[Security(check_auth)])
    async def crawl(request: CrawlRequest):
        return await application.state.resources.service.crawl(str(request.url), resolve_options(request, config))

    @application.post("/crawl/batch", response_model=BatchCrawlResponse, dependencies=[Security(check_auth)])
    async def batch(request: BatchCrawlRequest):
        started = time.monotonic()
        options = resolve_options(request, config)
        # All deadlines start at batch admission, including time behind max_concurrency.
        expires_at = time.monotonic() + options.timeout_ms / 1000
        semaphore = asyncio.Semaphore(request.max_concurrency)

        async def one(url):
            deadline = Deadline.at(expires_at)
            acquired = False
            try:
                await deadline.run(semaphore.acquire())
                acquired = True
                result = await application.state.resources.service.crawl(str(url), options, deadline)
                return BatchCrawlItemResult(
                    url=str(url),
                    success=result.success,
                    result=result,
                    error=None if result.success else _failure_reason(result),
                )
            except CrawlError as exc:
                if not acquired:
                    await application.state.resources.metrics.record(time.monotonic() - started, False)
                return BatchCrawlItemResult(url=str(url), success=False, error=str(exc))
            finally:
                if acquired:
                    semaphore.release()

        results = await asyncio.gather(*(one(url) for url in request.urls))
        succeeded = sum(item.success for item in results)
        return BatchCrawlResponse(
            total=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=results,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    return application


app = create_app()
