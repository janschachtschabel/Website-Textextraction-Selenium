"""Internal results: failed conversions never masquerade as extracted text."""

from dataclasses import dataclass, field
from typing import Literal

ExtractionStatus = Literal["ok", "empty", "unsupported", "skipped", "failed", "blocked"]


@dataclass
class ConversionResult:
    markdown: str = ""
    converter: str | None = None
    status: ExtractionStatus = "empty"
    warnings: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    data: bytes
    final_url: str
    status_code: int | None
    content_type: str | None
    engine: Literal["http", "selenium"] = "http"
    truncated: bool = False
    screenshot_base64: str | None = None
    warnings: list[str] = field(default_factory=list)
    settled: bool = True  # False: browser auto-wait gave up, the content may be incomplete
    validators: dict[str, str] = field(default_factory=dict)  # etag / last_modified of an HTTP response


class CrawlError(Exception):
    """A sanitized public message; never include downloaded text."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code
