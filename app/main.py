"""FastAPI boundary: authentication, schemas and endpoint adaptation."""

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .body_limit import BodySizeLimit
from .config import settings
from .logging_setup import setup_logging
from .resources import Resources
from .results import CrawlError
from .schemas import (
    BatchCrawlRequest,
    BatchCrawlResponse,
    CrawlRequest,
    CrawlResponse,
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
        return await application.state.resources.service.crawl_batch(
            [str(url) for url in request.urls], resolve_options(request, config), request.max_concurrency
        )

    return application


app = create_app()
