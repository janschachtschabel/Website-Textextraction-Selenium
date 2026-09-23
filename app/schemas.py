"""One option contract for single and batch requests.

Every field carries a description, and both request models carry an example that is
a request the service accepts: an undocumented field is rendered by /docs as a
placeholder for its type, which produced a body that failed validation and, once the
URL was corrected, crawled the site with "User-Agent: string".
"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from .config import TIMEOUT_CEILING_SECONDS, URL_CEILING, Settings, settings
from .results import CrawlError, ExtractionStatus

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


# The same request with every option named, for readers who want to see what there is.
# Verified against the running service: it crawls, renders, shoots and reports no warning.
CRAWL_FULL_EXAMPLE = {
    "url": "https://example.com",
    "mode": "js",
    "js_strategy": "accuracy",
    "timeout_ms": 120000,
    "retries": 1,
    "max_bytes": 10485760,
    # Empty, not null: the encoder drops a null from a published example, and the
    # validator reads an empty proxy as "none" anyway, so Execute still works.
    "proxy": "",
    "allow_insecure_ssl": False,
    "user_agent": "MyCrawler/1.0 (+https://example.org/bot)",
    "accept_language": "de,en;q=0.8",
    "headless": True,
    "js_auto_wait": True,
    "wait_for_selectors": ["body"],
    "wait_for_ms": 0,
    "html_converter": "trafilatura",
    "trafilatura_clean_markdown": True,
    "media_conversion_policy": "skip",
    "extract_links": True,
    "extract_metadata": True,
    "screenshot": True,
    "screenshot_full_page": True,
    "anonymize": False,
    "anonymize_language": "de",
    "crawl_rate_limit_rps": 1,
    "respect_robots_txt": True,
    "force_refresh": False,
}
BATCH_FULL_EXAMPLE = {key: value for key, value in CRAWL_FULL_EXAMPLE.items() if key != "url"} | {
    "urls": BATCH_EXAMPLE["urls"],
    "max_concurrency": 3,
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
JOB_EXAMPLE = {
    "job_id": "6l2fSU_R43ujmWd46UmHQA",
    "status": "queued",
    "status_url": "/jobs/6l2fSU_R43ujmWd46UmHQA",
    "results_url": "/jobs/6l2fSU_R43ujmWd46UmHQA/results",
}


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
    "progress": {"done": 2, "succeeded": 1, "total": 2},
    "results_url": JOB_EXAMPLE["results_url"],
    "error": None,
}

JOB_RESULTS_EXAMPLE = {
    "job_id": JOB_EXAMPLE["job_id"],
    "status": "running",
    "progress": {"done": 1, "succeeded": 1, "total": 2},
    "results": [{"position": 0, **ITEM_EXAMPLE}],
    "next_offset": 1,
}


class CrawlOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["fast", "js", "auto"] | None = Field(
        None,
        description="How the page is fetched. fast reads the HTTP response only; js always renders the "
        "page in Chrome; auto reads the HTTP response and renders in Chrome only when that yields a "
        "script-built shell: under 1000 characters of text with an app root such as #root, or a page "
        "asking for JavaScript. fetch_engine in the answer says which ran. Default: DEFAULT_MODE, auto",
    )
    js_strategy: Literal["accuracy", "speed"] | None = Field(
        None,
        description="How Chrome loads the page. accuracy waits for the full page load; speed reads it once "
        "the document is parsed and blocks image, font and media files by extension unless a screenshot "
        "is taken. With js_auto_wait, speed wants 0.3 s of still text instead of 1 s and gives up after "
        "10 s instead of 20 s. Default: DEFAULT_JS_STRATEGY, speed",
    )
    timeout_ms: int | None = Field(
        None,
        ge=1000,
        le=TIMEOUT_CEILING_SECONDS * 1000,
        description="Deadline for the whole request in milliseconds: waiting for capacity and for the "
        "host's rate limit counts, as do fetching, rendering and conversion. For a batch or job it "
        "covers all its URLs together. At most the operator's MAX_TIMEOUT_SECONDS, 600 unless raised; "
        "more is refused with 422. Default: DEFAULT_TIMEOUT_SECONDS, 120",
    )
    retries: int | None = Field(
        None,
        ge=0,
        le=10,
        description="How often a failed fetch is repeated, within timeout_ms. Over HTTP: after status "
        "429, 500, 502, 503 or 504 and after a connection error, waiting the Retry-After the site sends "
        "(30 s at most) or 1, 2, 4, then 8 s. In Chrome: after a navigation error, except a rejected "
        "certificate or a blocked request. A timeout is not repeated. Default: DEFAULT_RETRIES, 1",
    )
    max_bytes: int | None = Field(
        None,
        ge=1024,
        le=100 * 1024 * 1024,
        description="Most bytes read: the decoded HTTP body, or the HTML Chrome renders. A longer page "
        "is cut there, a warning says so, and the answer is not a success. It does not bound the "
        "screenshot. Default: DEFAULT_MAX_BYTES, 10 MiB",
    )
    proxy: str | None = Field(
        None,
        description="HTTP(S) proxy for this crawl, credentials in the URL allowed "
        "(http://user:password@host:port). It must accept CONNECT to numeric addresses: the service "
        "resolves and checks every destination itself. A proxy that does not resolve, or under the "
        "default network policy resolves to a private address, is refused with 400 before any connection",
    )
    allow_insecure_ssl: bool | None = Field(
        None,
        description="Accept a site whose TLS certificate does not validate, over HTTP and in Chrome. "
        "Default: ALLOW_INSECURE_SSL, false",
    )
    user_agent: str | None = Field(
        None,
        min_length=1,
        max_length=512,
        description="User-Agent header this crawl sends, printable ASCII. robots.txt is read for it too. "
        "Default: the operator's DEFAULT_USER_AGENT, which names this service and a contact URL",
    )
    accept_language: str | None = Field(
        None,
        max_length=256,
        description="Accept-Language header, such as de-DE,de;q=0.9. Empty sends none; Chrome gets it "
        "as its language list. Default: DEFAULT_ACCEPT_LANGUAGE, empty",
    )
    headless: bool | None = Field(
        None,
        description="Run Chrome without a window. Only rendering reads this; a container has no display, "
        "so keep it true there. Default: DEFAULT_HEADLESS, true",
    )
    js_auto_wait: bool | None = Field(
        None,
        description="In Chrome, wait until the page stops changing: its text still, no aria-busy or "
        "spinning progress bar left, mathematics typeset. It gives up after 10 s (speed) or 20 s "
        "(accuracy), counted once the selectors and the minimum wait are met, and the answer is then "
        "marked incomplete and not a success. Without it, a selector or wait_for_ms, the page is read "
        "as soon as it has loaded. Default: DEFAULT_JS_AUTO_WAIT, true",
    )
    wait_for_selectors: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="CSS selectors that must each match a visible element before the page is read. The "
        "wait has no limit of its own: a selector that never appears runs into timeout_ms (504). An "
        "invalid selector is refused with 422. Only rendering reads this",
    )
    wait_for_ms: int = Field(
        0,
        ge=0,
        le=30_000,
        description="Minimum wait in Chrome before the page is read, in milliseconds, within timeout_ms. "
        "Only rendering reads this",
    )
    html_converter: Literal["trafilatura", "markitdown", "bs4"] | None = Field(
        None,
        description="What turns HTML into Markdown. trafilatura extracts the main text, markitdown "
        "converts the whole document, bs4 takes its plain text. When one yields nothing the next is "
        "tried, in this order; converter in the answer names the one whose text you get. Default: "
        "HTML_CONVERTER, trafilatura",
    )
    trafilatura_clean_markdown: bool | None = Field(
        None,
        description="With trafilatura: true extracts the article as Markdown, with its links and "
        "tables and without navigation, headers or comments; false returns the whole page as plain "
        "text. The other converters ignore it. Default: TRAFILATURA_CLEAN_MARKDOWN, true",
    )
    media_conversion_policy: Literal["skip", "metadata", "full", "none"] | None = Field(
        None,
        description="What happens to audio and video. skip and none leave them unconverted "
        "(extraction_status skipped); metadata returns the ffprobe record as JSON, which needs ffprobe "
        "on the host - the published image has none, so there it fails with a warning; full is not "
        "implemented and answers unsupported. Default: MEDIA_CONVERSION_POLICY, skip",
    )
    extract_links: bool = Field(
        False,
        description="Return the page's links in links: absolute URL, text, whether it stays on the host, "
        "and a category such as content, nav or legal to filter by. HTML pages only; not with anonymize",
    )
    extract_metadata: bool = Field(
        False,
        description="Return what the page declares about itself in metadata: title, description, author, "
        "publication date, site name, canonical URL and language, read from its meta tags, JSON-LD and "
        "<html lang> by fixed rules. HTML pages only; not with anonymize",
    )
    screenshot: bool = Field(
        False,
        description="Return a PNG of the rendered page, base64 in screenshot_base64, up to 4000 by 20000 "
        "pixels. Only rendering takes one: mode=js always, auto only when it renders, fast never; a "
        "missing screenshot is named in warnings. Not with anonymize",
    )
    screenshot_full_page: bool = Field(
        False,
        description="The whole document instead of the visible viewport, clipped at 4000 by 20000 pixels. "
        "Needs screenshot",
    )
    anonymize: bool = Field(
        False,
        description="Replace personal data in the Markdown, such as names, places, e-mail addresses and "
        "phone numbers, using Presidio with a spaCy model; anonymization says what was found. Links, "
        "metadata and the screenshot are left out of such an answer. Needs the PII extra and the "
        "language's model, which the published image does not include: there it answers 503",
    )
    anonymize_language: Literal["de", "en"] = Field(
        "de",
        description="Language of the text for anonymize; each needs its spaCy model, set by "
        "PRESIDIO_DE_MODEL or PRESIDIO_EN_MODEL",
    )
    crawl_rate_limit_rps: float | None = Field(
        None,
        ge=0,
        le=100,
        description="Most requests per second to the target host, shared by every request to that host; "
        "0 sets no limit of your own. The operator's DEFAULT_DOMAIN_RATE_LIMIT_RPS, when set, is a "
        "ceiling: a lower value is honoured, a higher one or 0 is not. Default: that setting, 0 (off)",
    )
    respect_robots_txt: bool | None = Field(
        None,
        description="Read the host's robots.txt first and refuse the URL with 403 when it disallows the "
        "path for this crawl's User-Agent. Default: RESPECT_ROBOTS_TXT, false",
    )
    force_refresh: bool = Field(
        False,
        description="Fetch anew: ignore a cached result, which is kept RESULT_CACHE_TTL (300 s), and do "
        "not join an identical request already running",
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
    url: HttpUrl = Field(
        description="Address to crawl, http or https. The network policy checks where it resolves before "
        "the first request, and again for every redirect and for the address Chrome finally shows"
    )


class BatchCrawlRequest(CrawlOptions):
    model_config = ConfigDict(json_schema_extra={"examples": [BATCH_EXAMPLE]})
    urls: list[HttpUrl] = Field(
        min_length=1,
        max_length=URL_CEILING,
        description="Addresses to crawl, up to the operator's MAX_URLS_PER_REQUEST (50 unless raised); "
        "more are refused with 422. Each URL is admitted, cached and answered on its own, so one that "
        "fails does not fail the others",
    )
    max_concurrency: int = Field(
        3,
        ge=1,
        le=10,
        description="URLs of this batch fetched at once. Each takes a slot of the service-wide "
        "MAX_CONCURRENT_REQUESTS, which can hold it lower",
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
    # Refused rather than clamped: a bulk job silently cut to a shorter deadline fails on
    # its tail with nothing saying why, while this names the number to ask the operator for.
    if values["timeout_ms"] > config.max_timeout_seconds * 1000:
        raise CrawlError(f"timeout_ms above the {config.max_timeout_seconds} second limit of this service", 422)
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
    status_url: str = Field(description="Path to poll for the status and progress")
    results_url: str = Field(description="Path to read the results from, as they finish")


class JobProgress(BaseModel):
    """How far a job has got: counts, while it runs and after; the results themselves are
    read from results_url. Saved at most every two seconds, so it can trail the rows. A job
    that ends by itself saves its exact count; one ended by a shutdown or a lost process
    can report fewer URLs than it stored."""

    done: int = Field(description="URLs finished, whether they succeeded or not")
    succeeded: int = Field(description="Of those, how many produced a result")
    total: int = Field(description="URLs the job was given")


class JobStatus(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [JOB_STATUS_EXAMPLE]})
    job_id: str = Field(description="Identifier of the job")
    status: Literal["queued", "running", "done", "failed"] = Field(description="Where the job stands")
    submitted_at: str = Field(description="When the job was accepted, ISO 8601")
    finished_at: str | None = Field(None, description="When it finished, absent while it still runs")
    progress: JobProgress | None = Field(None, description="How many URLs are done, while the job runs and after")
    results_url: str = Field(description="Path to read the results from, while the job runs and after")
    error: str | None = Field(None, description="Why the job failed, absent otherwise")


class JobResultRow(BatchCrawlItemResult):
    model_config = ConfigDict(json_schema_extra={"examples": [{"position": 0, **ITEM_EXAMPLE}]})
    position: int = Field(description="Index of this URL in the list the job was given")


class JobResults(BaseModel):
    """URLs a job has finished, in the order they finished - which is not the order they
    were submitted in; each row's position says which URL of the request it was."""

    model_config = ConfigDict(json_schema_extra={"examples": [JOB_RESULTS_EXAMPLE]})
    job_id: str = Field(description="Identifier of the job")
    status: Literal["queued", "running", "done", "failed"] = Field(description="Where the job stands")
    progress: JobProgress | None = Field(None, description="How many URLs are done")
    results: list[JobResultRow] = Field(description="Rows from offset on, at most limit of them")
    next_offset: int = Field(
        description="The offset for the next page. An empty page from a job that is no longer "
        "queued or running means there is nothing more to read"
    )
