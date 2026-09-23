"""FastAPI boundary: authentication, schemas and endpoint adaptation.

The routes are registered in groups, one per concern, so this module's one function
that assembles the application stays readable as endpoints are added. Each group takes
what it needs rather than closing over everything the factory happens to have.
"""

import math
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Path, Query, Security
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from loguru import logger

from . import __version__
from .body_limit import BodySizeLimit
from .config import settings
from .error_docs import BATCH_REFUSED, NOT_A_JOB_ID, NOT_A_PAGE, STOPPING, TOO_MANY_JOBS, answers
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
    JobResults,
    JobStatus,
    resolve_options,
)
from .selenium_driver import browser_status
from .status_schemas import METRICS_EXAMPLE, Health, ServiceBanner, Stats

DESCRIPTION = """Turns a web page into Markdown: fetched over HTTP, or rendered in Chrome
when the page needs JavaScript to show its content.

**Start here.** `POST /crawl` with a body of `{"url": "https://..."}`. Every other option
falls back to the operator's default, so the example on that endpoint is a complete
request, not a template to fill in.

**Choosing an engine.** `mode=fast` never starts a browser, `mode=js` always does, and
`mode=auto` starts one only when the plain HTML yields too little text. A screenshot needs
the browser.

**Several URLs.** `POST /crawl/batch` takes as many as the operator allows - 50 unless
`MAX_URLS_PER_REQUEST` was raised - and answers once all of them are done. `POST /jobs`
takes the same body, answers at once with a job id, and is polled at `GET /jobs/{job_id}`;
its results arrive page by page at `GET /jobs/{job_id}/results` as the URLs finish. Use it
when a batch outlives the client's patience.

**Authentication.** When the operator configures an API key, every endpoint except `/` and
`/health` requires `Authorization: Bearer <key>`. This page needs it too: a browser is
asked for it as an HTTP Basic password, with any username.
"""


def _choices(minimal, complete, what):
    """Swagger offers these as a dropdown. The minimal body is the one to send; the
    complete one exists so every option is visible without leaving "Try it out"."""
    return {
        "minimal": {"summary": f"Minimal - {what}", "value": minimal},
        "complete": {"summary": "Every option named, at a working value", "value": complete},
    }


def _lifespan(config, resources):
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

    return lifespan


def _authenticator(config):
    bearer = HTTPBearer(auto_error=False)

    def check_auth(credentials: HTTPAuthorizationCredentials | None = Security(bearer)):
        if config.api_key and (
            credentials is None or not secrets.compare_digest(credentials.credentials.encode(), config.api_key.encode())
        ):
            raise HTTPException(401, "Invalid or missing API token", headers={"WWW-Authenticate": "Bearer"})

    return check_auth


def _docs_authenticator(config):
    """Bearer for tools, Basic for people.

    A browser cannot put a bearer token on a navigation, so behind Bearer alone the Swagger
    page would be unreachable by the only client that can use it. Basic makes the browser
    ask, and it then repeats the credentials for the page's own fetch of /openapi.json.
    The username is not checked; the password is the API key.
    """
    bearer = HTTPBearer(auto_error=False)
    basic = HTTPBasic(auto_error=False)

    def check_docs_auth(
        token: HTTPAuthorizationCredentials | None = Security(bearer),
        password: HTTPBasicCredentials | None = Security(basic),
    ):
        if not config.api_key:
            return
        supplied = token.credentials if token else (password.password if password else "")
        if not secrets.compare_digest(supplied.encode(), config.api_key.encode()):
            raise HTTPException(
                401,
                "Invalid or missing API token",
                headers={"WWW-Authenticate": 'Basic realm="Website Text Extraction"'},
            )

    return check_docs_auth


def _docs_routes(application, check_docs_auth):
    """Registered by hand, because FastAPI's built-in docs routes take no dependency and
    so can only be published or removed. These carry the same key as everything else."""
    guard = [Security(check_docs_auth)]

    @application.get("/openapi.json", include_in_schema=False, dependencies=guard)
    async def openapi_document():
        return application.openapi()

    @application.get("/docs", include_in_schema=False, dependencies=guard)
    async def swagger_ui():
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{application.title} - Swagger UI")

    @application.get("/redoc", include_in_schema=False, dependencies=guard)
    async def redoc():
        return get_redoc_html(openapi_url="/openapi.json", title=f"{application.title} - ReDoc")


def _admission(config):
    limit = (
        InboundLimit(config.inbound_rate_limit_rps, config.inbound_rate_limit_burst)
        if config.inbound_rate_limit_rps
        else None
    )

    def admit(urls: int):
        # Runs after authentication, so unauthenticated traffic cannot drain the bucket.
        if urls > config.max_urls_per_request:
            raise HTTPException(422, f"At most {config.max_urls_per_request} URLs per request")
        wait = limit.take(urls) if limit else 0
        if wait:
            raise HTTPException(429, "Too many crawl requests", headers={"Retry-After": str(math.ceil(wait))})

    return admit


def _public_routes(application, config):
    """Reachable without a key, so a probe can identify the service and watch it."""

    @application.get("/", summary="Service banner", response_model=ServiceBanner)
    async def root():
        """Name, version and the path to this documentation. Public, so a probe can identify
        the service without holding a key. The path is named even when the documentation
        behind it needs one: naming it costs nothing that guessing it does not."""
        return {"service": "Website Text Extraction", "version": __version__, "docs": "/docs"}

    @application.get(
        "/health",
        summary="Liveness, capacity and browser presence",
        response_model=Health,
        responses={503: {"model": Health, "description": STOPPING}},
    )
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


def _observability_routes(application, check_auth):
    """The same counters twice: as JSON for a person, as text for Prometheus."""

    @application.get(
        "/stats",
        dependencies=[Security(check_auth)],
        summary="Counters of the last hour",
        response_model=Stats,
        responses=answers(401),
    )
    async def stats():
        """Requests, errors, cache hits, coalesced answers and latency percentiles over a rolling
        60 minutes, with the throughput per minute, the entries in the result cache and the
        capacity in use. The counters cover every worker process of the deployment; the
        capacity is that of the process that answers."""
        active = application.state.resources
        count = await active.io(len, active.cache)
        result = await active.metrics.stats(count)
        result["capacity"] = active.service.capacity.stats()
        return result

    @application.get(
        "/metrics",
        dependencies=[Security(check_auth)],
        summary="The same counters for Prometheus",
        response_class=PlainTextResponse,
        responses={**answers(401), 200: {"content": {"text/plain": {"example": METRICS_EXAMPLE}}}},
    )
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


def _crawl_routes(application, config, admit, check_auth):
    """Crawling that answers when it is done; the caller waits."""

    @application.post(
        "/crawl",
        response_model=CrawlResponse,
        dependencies=[Security(check_auth)],
        summary="Crawl one URL",
        responses=answers(400, 401, 403, 413, 422, 429, 502, 503, 504),
    )
    async def crawl(
        request: CrawlRequest = Body(openapi_examples=_choices(CRAWL_EXAMPLE, CRAWL_FULL_EXAMPLE, "just the URL")),
    ):
        """Fetch one URL and return its text as Markdown.

        Only `url` is required; an option left out takes the operator's default, which its
        description names. The crawl runs in this order:

        1. The network policy checks where the URL resolves, and again for every redirect and
           for the address Chrome finally shows.
        2. The page is fetched over HTTP or rendered in Chrome. `mode` decides, and
           `fetch_engine` in the answer says which ran.
        3. The HTML or document is converted to Markdown; `converter` names what produced it.
        4. What was asked for besides the text is added: links, metadata, the screenshot
           Chrome took. With `anonymize`, personal data is replaced and those are left out.

        A successful answer is cached for `RESULT_CACHE_TTL` seconds, and identical requests
        running at once share one fetch; `cached` and `coalesced` say when that happened.
        `success` says whether the answer is usable content, and `warnings` lists everything
        that degraded it without failing it."""
        admit(1)
        return await application.state.resources.service.crawl(str(request.url), resolve_options(request, config))

    @application.post(
        "/crawl/batch",
        response_model=BatchCrawlResponse,
        dependencies=[Security(check_auth)],
        summary=f"Crawl up to {config.max_urls_per_request} URLs and wait",
        responses=answers(401, 413, 422, 429, specific={422: BATCH_REFUSED}),
    )
    async def batch(
        request: BatchCrawlRequest = Body(
            openapi_examples=_choices(BATCH_EXAMPLE, BATCH_FULL_EXAMPLE, "just the URLs")
        ),
    ):
        """Crawl a list of URLs and answer once the last one is done.

        The connection is held open for the whole batch, which can be minutes.
        `max_concurrency` bounds how many run at once; the service-wide capacity may hold
        it lower. A URL that fails carries its own reason and does not fail the others.
        `POST /jobs` does the same crawling and answers immediately instead."""
        admit(len(request.urls))
        return await application.state.resources.service.crawl_batch(
            [str(url) for url in request.urls], resolve_options(request, config), request.max_concurrency
        )


# The id becomes part of a state-store key, so it may only be what token_urlsafe produces:
# with a colon it could address a result row as if it were a job record.
JobId = Annotated[
    str,
    Path(
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description="The job_id that POST /jobs answered with: letters, digits, - and _, up to 64 "
        "characters. An unknown or expired id answers 404, any other string 422.",
    ),
]


def _job_routes(application, config, admit, check_auth):
    """The same crawling, delivered by a job id instead of a held-open connection."""

    @application.post(
        "/jobs",
        status_code=202,
        response_model=JobAccepted,
        dependencies=[Security(check_auth)],
        summary=f"Submit up to {config.max_urls_per_request} URLs as a background job",
        responses=answers(401, 413, 422, 429, 503, specific={422: BATCH_REFUSED, 503: TOO_MANY_JOBS}),
    )
    async def submit_job(
        request: BatchCrawlRequest = Body(
            openapi_examples=_choices(BATCH_EXAMPLE, BATCH_FULL_EXAMPLE, "just the URLs")
        ),
    ):
        """Take the body of /crawl/batch, answer at once with a job id and crawl in the background.

        Same body and the same crawling as `/crawl/batch`; only the delivery differs. Use
        it when the connection would not survive the wait: proxies and tunnels commonly cut
        a request at about 125 seconds, while a batch runs as long as its deadline allows.
        Poll `status_url` for progress and read the results from `results_url` as they
        finish; both stay readable for `JOB_RESULT_TTL` after the job ends."""
        admit(len(request.urls))
        job_id = await application.state.resources.jobs.submit(
            [str(url) for url in request.urls], resolve_options(request, config), request.max_concurrency
        )
        return JobAccepted(
            job_id=job_id, status="queued", status_url=f"/jobs/{job_id}", results_url=f"/jobs/{job_id}/results"
        )

    @application.get(
        "/jobs/{job_id}",
        response_model=JobStatus,
        dependencies=[Security(check_auth)],
        summary="Poll a background job",
        responses=answers(401, 404, 422, specific={422: NOT_A_JOB_ID}),
    )
    async def job_status(job_id: JobId):
        """Report how far a job has got, and where its results are.

        A job is `queued`, then `running`, then `done` once every URL has been tried - also
        when some of them failed; `progress` counts both. It is `failed` when the service
        shut down while it ran, when a result could not be stored, or when its process
        stopped: an unfinished job 30 seconds past its deadline is reported lost. `error`
        says which. The results themselves are read from `results_url`, while the job runs
        and after; both stay readable for `JOB_RESULT_TTL` seconds after the job ends."""
        record = await application.state.resources.jobs.status(job_id)
        if record is None:
            raise HTTPException(404, "Unknown or expired job")
        # .get, not [name]: a record written by an earlier release predates a field added
        # since, and the store outlives an upgrade.
        fields = {name: record.get(name) for name in JobStatus.model_fields if name not in {"job_id", "results_url"}}
        return JobStatus(job_id=job_id, results_url=f"/jobs/{job_id}/results", **fields)

    @application.get(
        "/jobs/{job_id}/results",
        response_model=JobResults,
        dependencies=[Security(check_auth)],
        summary="Read a job's results as they finish",
        responses=answers(401, 404, 422, specific={422: NOT_A_PAGE}),
    )
    async def job_results(
        job_id: JobId,
        offset: int = Query(0, ge=0, description="Rows already read: the previous page's next_offset"),
        limit: int = Query(20, ge=1, le=100, description="Rows in this page"),
    ):
        """Page through the URLs a job has finished, while it runs and after.

        Rows come in the order the URLs finished; `position` names the URL of the request.
        Start at `offset=0` and pass each page's `next_offset` on. An empty page from a job
        that is still running means nothing new yet; from one that has ended, nothing more.
        A row's Markdown is bounded by `max_bytes`, a screenshot only by its pixel limits,
        so a job that takes screenshots is best read with a small `limit`. Answers 404 when
        the id is unknown or its record has expired."""
        record, rows = await application.state.resources.jobs.rows(job_id, offset, limit)
        if record is None:
            raise HTTPException(404, "Unknown or expired job")
        return JobResults(
            job_id=job_id,
            status=record["status"],
            progress=record.get("progress"),
            results=rows,
            next_offset=offset + len(rows),
        )


def create_app(config=settings, resources=None):
    # The built-in docs routes are off because they cannot be protected; _docs_routes
    # registers the same three paths behind the API key instead.
    application = FastAPI(
        title="Website Text Extraction — Selenium",
        version=__version__,
        summary="Web pages as Markdown, with an optional browser for JavaScript sites",
        description=DESCRIPTION,
        lifespan=_lifespan(config, resources),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(BodySizeLimit, max_bytes=config.max_request_bytes)
    application.add_middleware(RequestId)  # added last, so it runs first and tags every answer
    check_auth = _authenticator(config)
    admit = _admission(config)

    @application.exception_handler(CrawlError)
    async def crawl_error_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    _public_routes(application, config)
    _docs_routes(application, _docs_authenticator(config))
    _observability_routes(application, check_auth)
    _crawl_routes(application, config, admit, check_auth)
    _job_routes(application, config, admit, check_auth)
    return application


app = create_app()
