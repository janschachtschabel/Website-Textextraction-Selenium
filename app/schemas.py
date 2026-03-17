from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class CrawlRequest(BaseModel):
    """
    Request schema for crawling web pages with automatic Markdown conversion.

    Supports three modes:
    - 'fast': Fast HTTP fetch for static content
    - 'js': Browser rendering for JavaScript-heavy pages
    - 'auto': Pre-flight analysis selects between HTTP_ONLY, JS_LIGHT or special paths
    """
    # OpenAPI example (default values shown in /docs)
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "url": "https://example.com",
                "mode": "auto",
                "js_strategy": "speed",
                "html_converter": "trafilatura",
                "trafilatura_clean_markdown": True,
                "media_conversion_policy": "skip",
                "allow_insecure_ssl": True,
                "extract_links": False,
                "anonymize": False,
                "anonymize_language": "de",
                "retries": 2,
                "timeout_ms": 30000,
                "max_bytes": 1048576,
                "force_refresh": False,
            }
        }
    )
    
    url: HttpUrl = Field(
        description="The URL to crawl",
        examples=["https://example.com", "https://docs.python.org/3/tutorial/"]
    )

    # HTML/Markdown conversion (per-request schema overrides)
    html_converter: Literal["trafilatura", "markitdown", "bs4"] | None = Field(
        default=None,
        description=(
            "HTML→Markdown converter for this request.\n"
            "• trafilatura (default in .env): Robust main-content extraction\n"
            "• markitdown: Full HTML→Markdown conversion\n"
            "• bs4: Simple text extraction (HTML only)"
        ),
        examples=[None, "trafilatura", "markitdown", "bs4"],
    )

    trafilatura_clean_markdown: bool | None = Field(
        default=None,
        description=(
            "Trafilatura output mode: True=clean Markdown (main content only), False=raw (html2txt).\n"
            "If None, the .env default (TRAFILATURA_CLEAN_MARKDOWN) is used."
        ),
        examples=[None, True, False],
    )

    media_conversion_policy: Literal["skip", "metadata", "full", "none"] | None = Field(
        default=None,
        description=(
            "Media conversion policy for audio/video:\n"
            "• skip (default): no conversion, short placeholder text\n"
            "• metadata: ffprobe metadata as JSON only\n"
            "• full: full conversion via markitdown/ffmpeg (slow)\n"
            "• none: no output at all for media files"
        ),
        examples=[None, "skip", "metadata", "full", "none"],
    )

    allow_insecure_ssl: bool | None = Field(
        default=None,
        description=(
            "Disable SSL validation for this request (verify=false).\n"
            "If None, the .env default (ALLOW_INSECURE_SSL) is used."
        ),
        examples=[None, True, False],
    )
    
    mode: Literal["fast", "js", "auto"] = Field(
        default="auto", 
        description="""Select crawl mode:

• fast: Fast HTTP fetch via httpx
  - For static HTML pages, PDFs, Office documents
  - HTTP/2, automatic redirects, cookie persistence
  - Fast and resource-efficient

• js: Browser rendering via Selenium Chrome
  - For JavaScript-heavy single-page applications
  - Waits for DOM content, dismisses cookie banners
  - Slower, but full browser behaviour

• auto: Pre-flight analysis (httpx + HTML parsing) selects the best strategy automatically
  - HTTP_ONLY: direct HTML (no Selenium)
  - JS_LIGHT: Selenium with aggressive "speed" profile (block assets, short waits)
  - Special cases: PDF/RSS/YouTube handled without Selenium""",
        examples=["auto", "fast", "js"]
    )
    
    js_strategy: Literal["accuracy", "speed"] = Field(
        default="speed",
        description="""Strategy for JS mode (Selenium):

• speed (default): aggressive acceleration with short caps and parallel waits (best effort)
• accuracy: maximum quality/robustness; slightly more conservative caps
""",
        examples=["accuracy", "speed"]
    )
    
    timeout_ms: int = Field(
        default=180_000, ge=1000, le=600_000,
        description="""Timeout in milliseconds (1–600 seconds):

• **fast mode**: HTTP request timeout
• **js mode**: Browser navigation + content wait time

Recommended values:
- Fast pages: 30,000 ms (30 s)
- Normal pages: 180,000 ms (3 min)
- Slow JS apps: 300,000 ms (5 min)""",
        examples=[30000, 180000, 300000]
    )
    
    retries: int = Field(
        default=2, ge=0, le=10, 
        description="""Number of retry attempts on failure:

• 0: No retry
• 1–3: Recommended for normal pages
• 4–10: For unstable or overloaded servers

Retried on: network errors, timeouts, 5xx server errors""",
        examples=[0, 2, 5]
    )
    
    proxy: str | None = Field(
        default=None, 
        description="""Optional proxy server:

Supported formats:
• HTTP: `http://proxy.example.com:8080`
• HTTPS: `https://proxy.example.com:8080`
• With auth: `http://user:pass@proxy.example.com:8080`
• SOCKS: `socks5://proxy.example.com:1080`

**Note**: Ignored when empty or the placeholder value "string" is used""",
        examples=[None, "http://proxy.example.com:8080", "http://user:pass@proxy.example.com:8080"]
    )
    
    max_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024,
        description="""Maximum response size in bytes:

Prevents oversized downloads and protects against memory issues.

Recommended values:
• Small pages: 1,048,576 (1 MB)
• Default: 10,485,760 (10 MB)
• Large documents: 52,428,800 (50 MB)

**Note**: Download stops when the limit is reached""",
        examples=[1048576, 10485760, 52428800]
    )
    
    extract_links: bool = Field(
        default=False, 
        description="""Link extraction for HTML content:

When enabled, all links on the page are extracted and categorised:

**Categories:**
• content: Content links (articles, resources)
• nav: Navigation (menus, breadcrumbs)
• social: Social media links
• auth: Login/registration
• legal: Imprint, privacy policy
• search: Search functions
• contact: Contact information
• download: Download links
• anchor: Anchor links (#section)
• other: Miscellaneous links

**Additional info**: URL, link text, internal/external""",
        examples=[False, True]
    )

    screenshot: bool = Field(
        default=True,
        description="""Capture a viewport screenshot of the rendered page (1920×1080 px, PNG as Base64).
Only available when Selenium is active (mode=js or mode=auto with JS path).
For mode=fast or auto+HTTP_ONLY, screenshot_base64 in the response will be null.""",
        examples=[True, False],
    )

    anonymize: bool = Field(
        default=False,
        description="""PII anonymisation via Presidio (local, no LLM, no external API call).
Detected entities (names, emails, phone numbers, addresses …) are replaced
by placeholders, e.g. <PERSON>, <EMAIL_ADDRESS>.
The anonymised text replaces the markdown field in the response.""",
        examples=[False, True],
    )
    anonymize_language: str = Field(
        default="de",
        description="Language for PII detection: de = German, en = English",
        examples=["de", "en"],
    )

    crawl_rate_limit_rps: float | None = Field(
        default=None,
        ge=0.0, le=100.0,
        description=(
            "Max requests per second to the target domain of this request.\n"
            "• None / not set: server default (DEFAULT_DOMAIN_RATE_LIMIT_RPS) applies\n"
            "• 0.0: disable per-domain limit for this request\n"
            "• > 0: overrides the server default for this domain"
        ),
        examples=[None, 0.0, 0.5, 1.0, 2.0],
    )

    force_refresh: bool = Field(
        default=False,
        description=(
            "Bypass the cache for this URL and fetch a fresh copy.\n"
            "• False (default): return cached result if available\n"
            "• True: skip cache lookup; freshly crawled content is stored in the cache\n"
            "Useful when a page has been updated since the last crawl."
        ),
        examples=[False, True],
    )


class BatchCrawlRequest(BaseModel):
    """Batch crawl for up to 50 URLs with shared settings.

    All parameters except `urls` and `max_concurrency` behave identically
    to the single `/crawl` endpoint and apply to every URL in the batch.
    """
    urls: List[HttpUrl] = Field(
        ..., min_length=1, max_length=50,
        description="List of URLs to crawl (1–50 entries)",
    )
    mode: Literal["fast", "js", "auto"] = Field(
        default="auto",
        description="Crawl mode for all URLs: auto (recommended), fast (HTTP only), js (Selenium)",
    )
    js_strategy: Literal["accuracy", "speed"] = Field(
        default="speed",
        description="Selenium strategy: speed (fast, best-effort) or accuracy (more robust)",
    )
    html_converter: Literal["trafilatura", "markitdown", "bs4"] | None = Field(
        default=None,
        description="HTML→Markdown converter: trafilatura (main content), markitdown (full), bs4 (simple). None = server default",
    )
    trafilatura_clean_markdown: bool | None = Field(
        default=None,
        description="Trafilatura output: True = clean (main content), False = raw, None = server default",
    )
    media_conversion_policy: Literal["skip", "metadata", "full", "none"] | None = Field(
        default=None,
        description="Audio/video handling: skip (placeholder), metadata (ffprobe info), full (conversion), none (no output)",
    )
    allow_insecure_ssl: bool | None = Field(
        default=None,
        description="Disable SSL validation (True = accept insecure certificates). None = server default",
    )
    timeout_ms: int = Field(
        default=60_000, ge=1000, le=600_000,
        description="Timeout per URL in milliseconds (default: 60 s). Increase for js mode if needed",
    )
    retries: int = Field(
        default=1, ge=0, le=5,
        description="Retry attempts per URL on network errors, timeouts or 5xx responses",
    )
    proxy: str | None = Field(
        default=None,
        description="Proxy for all URLs: http://host:port, https://host:port, socks5://host:port or with auth http://user:pass@host:port",
    )
    max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024,
        description="Maximum download size per URL in bytes (default: 5 MB). Protects against excessive memory usage",
    )
    extract_links: bool = Field(
        default=False,
        description="Extract and categorise links for all URLs (content, nav, social, auth, legal, download …)",
    )
    screenshot: bool = Field(
        default=True,
        description="Capture viewport screenshot (PNG as Base64). Only when Selenium is active (mode=js or auto+JS path)",
    )
    anonymize: bool = Field(
        default=False,
        description="PII anonymisation via Presidio for all URLs: names, emails, phone numbers etc. replaced by placeholders",
    )
    anonymize_language: str = Field(
        default="de",
        description="Language for PII detection: de (German) or en (English)",
    )
    crawl_rate_limit_rps: float | None = Field(
        default=None, ge=0.0, le=100.0,
        description="Max requests/s per target domain for all URLs. None = server default, 0 = no limit",
    )
    force_refresh: bool = Field(
        default=False,
        description=(
            "Bypass the cache for all URLs and fetch fresh copies.\n"
            "• False (default): return cached results if available\n"
            "• True: skip cache lookup; freshly crawled content is stored in the cache"
        ),
        examples=[False, True],
    )
    max_concurrency: int = Field(
        default=3, ge=1, le=10,
        description="Number of concurrent crawls within this batch (1–10). Higher values speed up the batch but increase server load",
    )


class BatchCrawlItemResult(BaseModel):
    """Result for a single URL entry in the batch."""
    url: str = Field(description="The crawled URL")
    success: bool = Field(description="True if the crawl succeeded")
    result: CrawlResponse | None = Field(default=None, description="Crawl result (only when success=true)")
    error: str | None = Field(default=None, description="Error message (only when success=false)")


class BatchCrawlResponse(BaseModel):
    """Response from the batch crawl endpoint."""
    total: int = Field(description="Total number of URLs crawled")
    succeeded: int = Field(description="Number of successfully crawled URLs")
    failed: int = Field(description="Number of failed URLs")
    results: List[BatchCrawlItemResult] = Field(description="Individual results in input order")
    elapsed_ms: int = Field(description="Total duration of the batch request in milliseconds")


class LinkInfo(BaseModel):
    """Information about an extracted link."""
    url: str = Field(description="Full URL of the link")
    text: str | None = Field(default=None, description="Visible link text")
    internal: bool = Field(description="True if the link belongs to the same domain")
    category: Literal[
        "content",
        "social",
        "nav",
        "auth",
        "legal",
        "search",
        "contact",
        "download",
        "anchor",
        "other",
    ] = Field(default="other", description="Automatically detected link category")


class AnonymizationResult(BaseModel):
    """Metadata of a Presidio anonymisation run."""
    entities_found: list[str] = Field(default_factory=list, description="Detected PII types (e.g. PERSON, EMAIL_ADDRESS)")
    entity_count: int = Field(default=0, description="Number of detected PII occurrences")
    warning: str | None = Field(default=None, description="Warning if anonymisation was incomplete")


class CrawlResponse(BaseModel):
    """Response schema for crawl requests."""
    request_mode: Literal["fast", "js", "auto"] = Field(description="Crawl mode that was used")
    requested_url: str = Field(description="Original URL from the request")
    final_url: str = Field(description="Final URL after redirects")
    status_code: int = Field(description="HTTP status code of the target page (200, 404, etc.)")
    redirected: bool = Field(description="Whether any redirects occurred")
    content_type: str | None = Field(description="MIME type of the response (text/html, application/pdf, etc.)")
    markdown: str = Field(description="Extracted Markdown content")
    markdown_length: int = Field(description="Character count of the Markdown text")
    word_count: int = Field(description="Word count of the Markdown text")
    error_page_detected: bool = Field(description="Whether an error page was detected (404, 403, captcha, thin/empty content, etc.)")
    links: list[LinkInfo] | None = Field(default=None, description="Extracted links (only when extract_links=true)")
    screenshot_base64: str | None = Field(default=None, description="Viewport screenshot as Base64-encoded PNG (only when screenshot=true and Selenium was used)")
    anonymization: AnonymizationResult | None = Field(default=None, description="Presidio anonymisation metadata (only when anonymize=true)")
    elapsed_ms: int = Field(description="Total request duration in milliseconds")
    cached: bool = Field(default=False, description="True if the result was served from the shared result cache")
