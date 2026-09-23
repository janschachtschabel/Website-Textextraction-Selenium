"""The answers of the routes that describe the service itself: banner, health and counters."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .prometheus import render

CAPACITY_EXAMPLE = {"active": 1, "waiting": 0, "limit": 8, "max_queue": 50}
HEALTH_EXAMPLE = {
    "status": "ok",
    "capacity": CAPACITY_EXAMPLE,
    "selenium": {"limit": 2, "started": 1, "busy": 0, "closed": False},
    "conversion": {"limit": 2, "started": 2, "busy": 1, "closed": False},
    "browser": {
        "chrome_binary": "/usr/bin/chromium",
        "chrome_binary_exists": True,
        "chromedriver_path": "/usr/bin/chromedriver",
        "chromedriver_exists": True,
    },
}
STATS_EXAMPLE = {
    "window_seconds": 3600,
    "window_resolution_seconds": 60,
    "requests_total": 42,
    "requests_success": 40,
    "requests_error": 2,
    "cache_hits": 5,
    "coalesced": 1,
    "cache_entries_current": 37,
    "latency_seconds": {"p50": 1, "p95": 5, "n": 34, "avg": 1.27, "approximate_percentiles": True},
    "throughput_per_minute": [0] * 57 + [12, 20, 10],
    "capacity": CAPACITY_EXAMPLE,
}
# Rendered by the code that answers /metrics, so the example cannot drift from it. The same
# crawls as STATS_EXAMPLE: 34 measured, counted into the duration buckets from 0.05 s to +Inf.
METRICS_EXAMPLE = render(
    {
        "requests": 42,
        "errors": 2,
        "cached": 5,
        "coalesced": 1,
        "n": 34,
        "sum": 43.2,
        "max": 9.8,
        "histogram": [0, 0, 2, 9, 12, 6, 4, 1, 0, 0, 0, 0, 0, 0],
    },
    {"extraction_ready": ("1 while the service accepts work", 1)},
).decode()


class ServiceBanner(BaseModel):
    service: str = Field(description="Name of the service")
    version: str = Field(description="Release that answers")
    docs: str = Field(description="Path of this documentation; behind the API key when the operator set one")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"service": "Website Text Extraction", "version": __version__, "docs": "/docs"}]
        }
    )


class Capacity(BaseModel):
    """Admission per URL, counted in the worker process that answers."""

    active: int = Field(description="URLs being fetched or converted right now")
    waiting: int = Field(description="URLs waiting for a free slot")
    limit: int = Field(description="URLs worked on at once: MAX_CONCURRENT_REQUESTS")
    max_queue: int = Field(
        description="URLs that may wait: MAX_QUEUE_SIZE. One more is refused with 503; one that waits "
        "longer than QUEUE_TIMEOUT_SECONDS gets 504"
    )


class Pool(BaseModel):
    limit: int = Field(description="Worker processes the pool may run: SELENIUM_MAX_POOL_SIZE or CONVERSION_WORKERS")
    started: int = Field(
        description="Worker processes running; each is replaced after WORKER_MAX_JOBS successful tasks, "
        "which bounds the memory it can accumulate"
    )
    busy: int = Field(description="Workers running a task right now")
    closed: bool = Field(description="True once the pool shuts down and takes no more tasks")


class Browser(BaseModel):
    chrome_binary: str | None = Field(
        None,
        description="CHROME_BINARY; null lets Selenium Manager find Chrome. Absent when the operator set API_KEY",
    )
    chrome_binary_exists: bool | None = Field(description="Whether that file exists; null when no path is set")
    chromedriver_path: str | None = Field(
        None,
        description="CHROMEDRIVER_PATH; null lets Selenium Manager find the driver. Absent when the operator "
        "set API_KEY",
    )
    chromedriver_exists: bool | None = Field(description="Whether that file exists; null when no path is set")


class Health(BaseModel):
    status: Literal["ok", "stopping"] = Field(
        description="ok while the service accepts work; stopping, answered with 503, once it shuts down"
    )
    capacity: Capacity = Field(description="URLs in work and waiting, in the worker process that answered")
    selenium: Pool = Field(description="The browser workers, each driving one Chrome")
    conversion: Pool = Field(
        description="The conversion workers: HTML and documents to Markdown, links, metadata and anonymization"
    )
    browser: Browser = Field(
        description="Whether the configured Chrome and ChromeDriver exist; their paths only without API_KEY"
    )

    model_config = ConfigDict(json_schema_extra={"examples": [HEALTH_EXAMPLE]})


class Latency(BaseModel):
    p50: float | None = Field(
        description="Median in seconds: the upper bound of the histogram bucket it falls in, capped at the "
        "slowest crawl; null without a measured crawl"
    )
    p95: float | None = Field(description="95th percentile in seconds, read the same way")
    n: int = Field(description="Crawls measured: those that succeeded with a fresh fetch")
    avg: float | None = Field(description="Mean in seconds; null without a measured crawl")
    approximate_percentiles: bool = Field(
        description="Always true: the percentiles come from fixed buckets between 0.05 and 600 seconds"
    )


class Stats(BaseModel):
    window_seconds: int = Field(description="Span the counters cover: the last hour")
    window_resolution_seconds: int = Field(description="Width of one bucket: the window moves minute by minute")
    requests_total: int = Field(
        description="URLs crawled in the window by all worker processes: each /crawl, and each URL of a batch or job"
    )
    requests_success: int = Field(description="Of those, the ones that succeeded")
    requests_error: int = Field(description="Of those, the ones that failed")
    cache_hits: int = Field(description="Answers taken from the result cache instead of a fetch")
    coalesced: int = Field(description="Answers shared from an identical request that was already running")
    cache_entries_current: int = Field(description="Entries in the result cache now")
    latency_seconds: Latency = Field(
        description="Time to crawl a URL that succeeded with a fresh fetch; cache hits and shared answers are left out"
    )
    throughput_per_minute: list[int] = Field(
        description="URLs crawled per minute, 60 values: the oldest minute first, the current one last"
    )
    capacity: Capacity = Field(description="URLs in work and waiting, in the worker process that answered")

    model_config = ConfigDict(json_schema_extra={"examples": [STATS_EXAMPLE]})
