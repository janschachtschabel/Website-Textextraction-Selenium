"""One option contract for single and batch requests."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .config import Settings, settings
from .results import ExtractionStatus


class CrawlOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["fast", "js", "auto"] | None = None
    js_strategy: Literal["accuracy", "speed"] | None = None
    timeout_ms: int | None = Field(None, ge=1000, le=600_000, description="End-to-end deadline including queue time")
    retries: int | None = Field(None, ge=0, le=10)
    max_bytes: int | None = Field(
        None,
        ge=1024,
        le=100 * 1024 * 1024,
        description="Decoded HTTP body or rendered HTML limit; truncation is reported",
    )
    proxy: str | None = Field(None, description="HTTP(S) proxy; numeric CONNECT destinations must be supported")
    allow_insecure_ssl: bool | None = None
    user_agent: str | None = Field(None, min_length=1, max_length=512)
    accept_language: str | None = Field(
        None, max_length=256, description="Accept-Language header; empty sends none. Chrome gets the language list"
    )
    headless: bool | None = None
    js_auto_wait: bool | None = None
    wait_for_selectors: list[str] = Field(
        default_factory=list, max_length=10, description="All selectors must become visible"
    )
    wait_for_ms: int = Field(0, ge=0, le=30_000, description="Optional minimum wait, within the request deadline")
    html_converter: Literal["trafilatura", "markitdown", "bs4"] | None = None
    trafilatura_clean_markdown: bool | None = None
    media_conversion_policy: Literal["skip", "metadata", "full", "none"] | None = None
    extract_links: bool = False
    extract_metadata: bool = False
    screenshot: bool = False
    anonymize: bool = False
    anonymize_language: Literal["de", "en"] = "de"
    crawl_rate_limit_rps: float | None = Field(None, ge=0, le=100)
    force_refresh: bool = False

    @field_validator("proxy")
    @classmethod
    def valid_proxy(cls, value):
        if not value or value == "string":
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
    url: HttpUrl


class BatchCrawlRequest(CrawlOptions):
    urls: list[HttpUrl] = Field(min_length=1, max_length=50)
    max_concurrency: int = Field(3, ge=1, le=10)


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
    url: str
    text: str | None = None
    internal: bool
    category: Literal[
        "content", "social", "nav", "auth", "legal", "search", "contact", "download", "anchor", "other"
    ] = "other"


class PageMetadata(BaseModel):
    title: str | None = None
    description: str | None = None
    author: str | None = None
    date: str | None = Field(None, description="Publication date as YYYY-MM-DD")
    site_name: str | None = Field(None, description="Declared site name, else the host name")
    canonical_url: str | None = Field(None, description="Declared canonical URL, else the final URL")
    language: str | None = Field(None, description="The <html lang> attribute")


class AnonymizationResult(BaseModel):
    entities_found: list[str] = Field(default_factory=list)
    entity_count: int = 0
    warning: str | None = None


class CrawlResponse(BaseModel):
    request_mode: Literal["fast", "js", "auto"]
    fetch_engine: Literal["http", "selenium"]
    converter: str | None = None
    extraction_status: ExtractionStatus
    success: bool
    requested_url: str
    final_url: str
    status_code: int | None = Field(description="Upstream status; null when the browser cannot observe it")
    redirected: bool
    content_type: str | None
    markdown: str
    markdown_length: int
    word_count: int
    error_page_detected: bool
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    links: list[LinkInfo] | None = None
    metadata: PageMetadata | None = None
    screenshot_base64: str | None = None
    anonymization: AnonymizationResult | None = None
    elapsed_ms: int
    cached: bool = False
    coalesced: bool = False


class BatchCrawlItemResult(BaseModel):
    url: str
    success: bool
    result: CrawlResponse | None = None
    error: str | None = None


class BatchCrawlResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    results: list[BatchCrawlItemResult]
    elapsed_ms: int
