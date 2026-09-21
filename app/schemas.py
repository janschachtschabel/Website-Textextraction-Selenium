"""One option contract for single and batch requests.

Every field carries a description, and both request models carry an example that is
a request the service accepts: an undocumented field is rendered by /docs as a
placeholder for its type, which produced a body that failed validation and, once the
URL was corrected, crawled the site with "User-Agent: string".
"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from .config import Settings, settings
from .results import ExtractionStatus

# The body /docs offers for "Try it out". Minimal on purpose: every option left out
# falls back to the operator's default, so this is the shortest request that works.
CRAWL_EXAMPLE = {
    "url": "https://en.wikipedia.org/wiki/Photosynthesis",
    "mode": "auto",
    "extract_metadata": True,
}
BATCH_EXAMPLE = {
    "urls": [
        "https://en.wikipedia.org/wiki/Photosynthesis",
        "https://en.wikipedia.org/wiki/Pythagorean_theorem",
    ],
    "max_concurrency": 3,
    "extract_metadata": True,
}


# A real answer, from a crawl of example.com, so the shape and the counts are honest
# rather than a placeholder of "string" and 0 for every field.
CRAWL_ANSWER_EXAMPLE = {
    "request_mode": "fast",
    "fetch_engine": "http",
    "converter": "trafilatura",
    "extraction_status": "ok",
    "success": True,
    "requested_url": "https://example.com/",
    "final_url": "https://example.com/",
    "status_code": 200,
    "redirected": False,
    "content_type": "text/html",
    "markdown": "# Example Domain\n\nThis domain is for use in documentation examples without needing "
    "permission. Avoid use in operations.\n\nLearn more",
    "markdown_length": 131,
    "word_count": 20,
    "error_page_detected": False,
    "truncated": False,
    "warnings": [],
    "links": [
        {"url": "https://iana.org/domains/example", "text": "Learn more", "internal": False, "category": "content"}
    ],
    "metadata": {
        "title": "Example Domain",
        "description": None,
        "author": None,
        "date": None,
        "site_name": "example.com",
        "canonical_url": "https://example.com/",
        "language": "en",
    },
    "screenshot_base64": None,
    "anonymization": None,
    "elapsed_ms": 412,
    "cached": False,
    "coalesced": False,
    "revalidated": False,
}
JOB_EXAMPLE = {"job_id": "6l2fSU_R43ujmWd46UmHQA", "status": "queued", "status_url": "/jobs/6l2fSU_R43ujmWd46UmHQA"}


ITEM_EXAMPLE = {
    "url": "https://example.com/",
    "success": True,
    "result": CRAWL_ANSWER_EXAMPLE,
    "error": None,
}
# A URL the service refused before crawling carries no result; one that was crawled but
# yielded nothing usable, a 404 among them, carries both its result and the reason.
REFUSED_ITEM_EXAMPLE = {
    "url": "https://example.com/private/",
    "success": False,
    "result": None,
    "error": "Disallowed by robots.txt",
}
BATCH_ANSWER_EXAMPLE = {
    "total": 2,
    "succeeded": 1,
    "failed": 1,
    "results": [ITEM_EXAMPLE, REFUSED_ITEM_EXAMPLE],
    "elapsed_ms": 780,
}
JOB_STATUS_EXAMPLE = {
    "job_id": JOB_EXAMPLE["job_id"],
    "status": "done",
    "submitted_at": "2026-09-21T07:17:14+00:00",
    "finished_at": "2026-09-21T07:17:16+00:00",
    "result": BATCH_ANSWER_EXAMPLE,
    "error": None,
}


class CrawlOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["fast", "js", "auto"] | None = Field(
        None,
        description="fast reads the HTTP response only, js always renders the page in Chrome, "
        "auto renders it when the plain HTML yields too little",
    )
    js_strategy: Literal["accuracy", "speed"] | None = Field(
        None,
        description="accuracy waits for the page to settle; speed shortens that wait and blocks "
        "images, fonts and media, unless a screenshot is requested",
    )
    timeout_ms: int | None = Field(None, ge=1000, le=600_000, description="End-to-end deadline including queue time")
    retries: int | None = Field(None, ge=0, le=10, description="Repeats of a failed fetch, all within the deadline")
    max_bytes: int | None = Field(
        None,
        ge=1024,
        le=100 * 1024 * 1024,
        description="Decoded HTTP body or rendered HTML limit; truncation is reported",
    )
    proxy: str | None = Field(None, description="HTTP(S) proxy; numeric CONNECT destinations must be supported")
    allow_insecure_ssl: bool | None = Field(
        None, description="Continue even when the TLS certificate of the site does not validate"
    )
    user_agent: str | None = Field(
        None,
        min_length=1,
        max_length=512,
        description="User-Agent this crawl sends; the operator's default applies when omitted",
    )
    accept_language: str | None = Field(
        None, max_length=256, description="Accept-Language header; empty sends none. Chrome gets the language list"
    )
    headless: bool | None = Field(None, description="Run Chrome without a window. Only the browser engine reads this")
    js_auto_wait: bool | None = Field(
        None,
        description="Wait until the page stops changing. Without it, and without a selector or a "
        "minimum wait, the page is read as soon as it loads",
    )
    wait_for_selectors: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="CSS selectors that must all become visible before the page is read",
    )
    wait_for_ms: int = Field(0, ge=0, le=30_000, description="Optional minimum wait, within the request deadline")
    html_converter: Literal["trafilatura", "markitdown", "bs4"] | None = Field(
        None,
        description="trafilatura extracts the article, markitdown converts the whole document, bs4 "
        "is the plain-text fallback. One that yields nothing falls back to the next",
    )
    trafilatura_clean_markdown: bool | None = Field(
        None, description="Extract the article instead of the whole page text. Only trafilatura reads this"
    )
    media_conversion_policy: Literal["skip", "metadata", "full", "none"] | None = Field(
        None,
        description="For audio and video: skip and none refuse them, metadata returns the ffprobe "
        "record. full is not implemented and is refused as unsupported",
    )
    extract_links: bool = Field(False, description="Return the page's links, categorised and made absolute")
    extract_metadata: bool = Field(False, description="Return title, author, date, canonical URL and language")
    screenshot: bool = Field(
        False, description="Return a PNG of the page as base64. Needs the browser, so mode=js or auto"
    )
    screenshot_full_page: bool = Field(
        False, description="Whole document instead of the viewport, clipped at 4000 by 20000 pixels"
    )
    anonymize: bool = Field(
        False,
        description="Remove personal data from the text. Links, metadata and the screenshot are "
        "suppressed for an anonymised answer",
    )
    anonymize_language: Literal["de", "en"] = Field("de", description="Language of the recogniser used to anonymise")
    crawl_rate_limit_rps: float | None = Field(
        None,
        ge=0,
        le=100,
        description="Requests per second against the target host. The operator's limit is a ceiling; "
        "a lower value is honoured, a higher one is not",
    )
    respect_robots_txt: bool | None = Field(
        None, description="Ask the host's robots.txt first and answer 403 when it disallows the path"
    )
    force_refresh: bool = Field(
        False, description="Fetch anew instead of answering from the cache or from a request already running"
    )

    @model_validator(mode="after")
    def full_page_needs_a_screenshot(self):
        if self.screenshot_full_page and not self.screenshot:
            raise ValueError("screenshot_full_page requires screenshot")
        return self

    @field_validator("proxy")
    @classmethod
    def valid_proxy(cls, value):
        if not value:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Only HTTP(S) proxy URLs are supported")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid proxy port")
        return value

    @field_validator("user_agent", "accept_language")
    @classmethod
    def valid_header_value(cls, value):
        if value and any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError("Header values must be printable ASCII")
        return value


class CrawlRequest(CrawlOptions):
    model_config = ConfigDict(json_schema_extra={"examples": [CRAWL_EXAMPLE]})
    url: HttpUrl = Field(description="Address to crawl; http and https only")


class BatchCrawlRequest(CrawlOptions):
    model_config = ConfigDict(json_schema_extra={"examples": [BATCH_EXAMPLE]})
    urls: list[HttpUrl] = Field(min_length=1, max_length=50, description="Addresses to crawl, 1 to 50 per request")
    max_concurrency: int = Field(
        3, ge=1, le=10, description="URLs fetched at once, within the service's global capacity"
    )


def resolve_options(request: CrawlOptions, config: Settings = settings) -> CrawlOptions:
    values = {name: getattr(request, name) for name in CrawlOptions.model_fields}
    defaults = {
        "mode": config.default_mode,
        "js_strategy": config.default_js_strategy,
        "timeout_ms": config.default_timeout_seconds * 1000,
        "retries": config.default_retries,
        "max_bytes": config.default_max_bytes,
        "headless": config.default_headless,
        "user_agent": config.default_user_agent,
        "accept_language": config.default_accept_language,
        "js_auto_wait": config.default_js_auto_wait,
        "html_converter": config.html_converter,
        "trafilatura_clean_markdown": config.trafilatura_clean_markdown,
        "media_conversion_policy": config.media_conversion_policy,
        "allow_insecure_ssl": config.allow_insecure_ssl,
        "crawl_rate_limit_rps": config.default_domain_rate_limit_rps,
        "respect_robots_txt": config.respect_robots_txt,
    }
    values.update({name: value for name, value in defaults.items() if values[name] is None})
    # A politeness limit the operator sets is a ceiling: a client may crawl a domain more
    # slowly, but not faster, and cannot switch the limit off with 0.
    ceiling = config.default_domain_rate_limit_rps
    if ceiling > 0:
        requested = values["crawl_rate_limit_rps"]
        values["crawl_rate_limit_rps"] = min(requested, ceiling) if requested else ceiling
    return CrawlOptions(**values)


class LinkInfo(BaseModel):
    url: str = Field(description="Absolute URL of the link")
    text: str | None = Field(None, description="Link text, absent when the link wraps an image only")
    internal: bool = Field(description="True while the link stays on the host of the crawled page")
    category: Literal[
        "content", "social", "nav", "auth", "legal", "search", "contact", "download", "anchor", "other"
    ] = Field("other", description="What the link looks like by target and text, so callers can filter")


class PageMetadata(BaseModel):
    title: str | None = Field(None, description="Declared title, else the <title> element")
    description: str | None = Field(None, description="Summary the page declares for search engines")
    author: str | None = Field(None, description="Declared author of the page")
    date: str | None = Field(None, description="Publication date as YYYY-MM-DD")
    site_name: str | None = Field(None, description="Declared site name, else the host name")
    canonical_url: str | None = Field(None, description="Declared canonical URL, else the final URL")
    language: str | None = Field(None, description="The <html lang> attribute")


class AnonymizationResult(BaseModel):
    entities_found: list[str] = Field(default_factory=list, description="Kinds of personal data that were replaced")
    entity_count: int = Field(0, description="How many occurrences were replaced")
    warning: str | None = Field(None, description="Why the anonymisation is incomplete, when it is")


class CrawlResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [CRAWL_ANSWER_EXAMPLE]})
    request_mode: Literal["fast", "js", "auto"] = Field(description="Mode used, after the operator's defaults")
    fetch_engine: Literal["http", "selenium"] = Field(
        description="http for the plain response, selenium when Chrome rendered the page"
    )
    converter: str | None = Field(None, description="Converter that produced the Markdown, null when none succeeded")
    extraction_status: ExtractionStatus = Field(description="Outcome of the conversion step")
    success: bool = Field(description="True when the crawl produced usable text")
    requested_url: str = Field(description="URL as it was requested")
    final_url: str = Field(description="URL after all redirects")
    status_code: int | None = Field(description="Upstream status; null when the browser cannot observe it")
    redirected: bool = Field(description="True when the final URL differs from the requested one")
    content_type: str | None = Field(description="Content-Type the upstream declared")
    markdown: str = Field(description="The extracted text as Markdown")
    markdown_length: int = Field(description="Characters in the Markdown")
    word_count: int = Field(description="Words in the Markdown")
    error_page_detected: bool = Field(description="True when the text reads as an error or block page, not content")
    truncated: bool = Field(False, description="True when the body reached max_bytes and was cut")
    warnings: list[str] = Field(default_factory=list, description="What degraded the result without failing it")
    links: list[LinkInfo] | None = Field(None, description="Present when extract_links was set")
    metadata: PageMetadata | None = Field(None, description="Present when extract_metadata was set")
    screenshot_base64: str | None = Field(None, description="PNG as base64, present when a screenshot was taken")
    anonymization: AnonymizationResult | None = Field(None, description="Present when anonymize was set")
    elapsed_ms: int = Field(description="Time from accepting the request to answering it")
    cached: bool = Field(False, description="Answered from the result cache")
    coalesced: bool = Field(False, description="Answered from a request for the same URL already in flight")
    revalidated: bool = Field(False, description="Cached result confirmed unchanged by the upstream (304)")


class BatchCrawlItemResult(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [ITEM_EXAMPLE]})
    url: str = Field(description="The URL this entry reports on")
    success: bool = Field(description="True when this URL produced usable text")
    result: CrawlResponse | None = Field(
        None,
        description="The crawl result. Absent only when the URL was refused before it was "
        "crawled; a page that answered 404 carries both its result and the reason",
    )
    error: str | None = Field(None, description="Why this URL failed, absent when it succeeded")


class BatchCrawlResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [BATCH_ANSWER_EXAMPLE]})
    total: int = Field(description="URLs in the request")
    succeeded: int = Field(description="URLs that produced usable text")
    failed: int = Field(description="URLs that did not")
    results: list[BatchCrawlItemResult] = Field(description="One entry per URL, in the order requested")
    elapsed_ms: int = Field(description="Time from accepting the batch to answering it")


class JobAccepted(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [JOB_EXAMPLE]})
    job_id: str = Field(description="Identifier to poll the job with")
    status: Literal["queued"] = Field(description="A new job is always queued")
    status_url: str = Field(description="Path to poll for the result")


class JobStatus(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [JOB_STATUS_EXAMPLE]})
    job_id: str = Field(description="Identifier of the job")
    status: Literal["queued", "running", "done", "failed"] = Field(description="Where the job stands")
    submitted_at: str = Field(description="When the job was accepted, ISO 8601")
    finished_at: str | None = Field(None, description="When it finished, absent while it still runs")
    result: BatchCrawlResponse | None = Field(None, description="The batch result, present once the job is done")
    error: str | None = Field(None, description="Why the job failed, absent otherwise")
