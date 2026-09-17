"""In-memory HTML conversion, with an explicit record of the converter used."""

import io
import re
import threading
from copy import copy
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from trafilatura import extract, html2txt

from .embedded_content import embedded_html
from .markup import enhance_table_structure, prepare_html
from .results import ConversionResult

_local = threading.local()
_HIDDEN_CLASSES = {"sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text", "hidden"}


def markitdown_stream(data: bytes, content_type: str | None, extension: str, url: str | None) -> str:
    from markitdown import MarkItDown, StreamInfo

    if not hasattr(_local, "markitdown"):
        _local.markitdown = MarkItDown(enable_plugins=False)
    result = _local.markitdown.convert_stream(
        # URL-based converters can perform their own unguarded network requests.
        io.BytesIO(data),
        stream_info=StreamInfo(mimetype=content_type, extension=extension),
    )
    return result.text_content.strip()


def page_heading(soup: BeautifulSoup) -> str:
    """Text of the first visible <h1>; empty for screen-reader helpers and unclosed, overlong headings."""
    for h1 in soup.find_all("h1"):
        hidden = h1.has_attr("hidden") or str(h1.get("aria-hidden", "")).lower() == "true"
        if hidden or _HIDDEN_CLASSES & set(h1.get("class", [])):
            continue
        heading = copy(h1)
        for br in heading.find_all("br"):
            br.replace_with(" ")
        text = " ".join(heading.get_text().split())  # inline markup (H<sub>2</sub>O) adds no spaces
        return text if len(text) <= 200 else ""
    return ""


def _compact(text: str) -> str:
    # Letters and digits only, so Markdown emphasis or link targets do not hide a kept heading.
    return re.sub(r"[\W_]+", "", re.sub(r"\]\([^)]*\)", "", text)).casefold()


def with_heading(text: str | None, heading: str) -> str | None:
    # Trafilatura keeps the page heading only when it sits inside the detected main content.
    if text and heading and not text.lstrip().startswith("# ") and _compact(heading) not in _compact(text):
        return f"# {heading}\n\n{text}"
    return text


def convert_html(
    data: bytes, content_type: str | None, url: str | None, converter: str, clean: bool
) -> ConversionResult:
    soup = prepare_html(data, content_type)
    embedded = embedded_html(soup, url)
    if embedded:
        soup = prepare_html(embedded.encode("utf-8"), "text/html; charset=utf-8")
        # The payload is already the selected content, including attachment links.
        # Main-content heuristics can discard links from short lesson fragments.
        if converter == "trafilatura":
            converter = "markitdown"
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    base_tag = soup.find("base", href=True)
    base = urljoin(url or "", base_tag["href"] if base_tag else "")
    for tag in soup.select("[href], [src]"):
        for attribute in ("href", "src"):
            if tag.get(attribute):
                tag[attribute] = urljoin(base, tag[attribute])
    if not soup.get_text(" ", strip=True):
        return ConversionResult()
    html = str(soup)
    heading = page_heading(soup)
    warnings = []
    candidates = [converter] + (["markitdown", "bs4"] if converter == "trafilatura" else ["bs4"])
    for candidate in dict.fromkeys(candidates):
        try:
            if candidate == "trafilatura":
                text = (
                    with_heading(
                        extract(
                            html,
                            url=url,
                            output_format="markdown",
                            include_links=True,
                            include_tables=True,
                            include_comments=False,
                        ),
                        heading,
                    )
                    if clean
                    else html2txt(html)
                )
            elif candidate == "markitdown":
                text = markitdown_stream(html.encode(), "text/html; charset=utf-8", ".html", url)
            else:
                text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
        except Exception as exc:
            warnings.append(f"{candidate} failed ({type(exc).__name__}); trying fallback")
            continue
        if text and text.strip():
            return ConversionResult(enhance_table_structure(text.strip()), candidate, "ok", warnings)
    return ConversionResult(status="failed", warnings=warnings or ["No converter produced text"])
