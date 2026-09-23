"""In-memory HTML conversion, with an explicit record of the converter used."""

import io
import re
import threading
from copy import copy
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from trafilatura import extract, html2txt

from .embedded_content import embedded_html
from .links import DOWNLOAD_EXTS, absolute_url
from .markup import HIDDEN_STYLE, enhance_table_structure, prepare_html
from .results import ConversionResult

_local = threading.local()
_HIDDEN_CLASSES = {"sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text", "hidden"}
# A file linked from the page's navigation, banner, footer or sidebar is the site's, not the page's.
_CHROME_ROLES = {"navigation", "banner", "contentinfo", "complementary"}
_SECTIONING = ["article", "aside", "main", "nav", "section"]
_FILE_EXTENSIONS = tuple(DOWNLOAD_EXTS)


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


def _outside_content(tag) -> bool:
    """Hidden from readers - as Wikipedia's archive placeholders are - or a landmark around the
    page's content, as browsers map them: a header or footer is the page's only outside article,
    aside, main, nav and section, and an aside is a sidebar only outside article and section."""
    hidden = (
        tag.get("hidden") not in (None, "until-found")
        or str(tag.get("aria-hidden", "")).lower() == "true"
        or bool(HIDDEN_STYLE.search(tag.get("style", "")))
    )
    if hidden or tag.name == "nav" or tag.get("role") in _CHROME_ROLES:
        return True
    if tag.name in {"header", "footer"}:
        return tag.find_parent(_SECTIONING) is None
    if tag.name == "aside":
        return tag.find_parent(["article", "section"]) is None
    return False


def document_links(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Label and URL of each file the page's content links to, in page order: an http(s) link whose
    path ends in one of DOWNLOAD_EXTS, which `links` reports as a download.

    Expects the prepared document, whose references are already absolute. A link without text is
    labelled with its aria-label, its title or its file name."""
    found = {}
    for anchor in soup.find_all("a", href=True):
        url = anchor["href"].strip()
        parts = urlsplit(url)
        if url in found or parts.scheme not in {"http", "https"} or not parts.path.lower().endswith(_FILE_EXTENSIONS):
            continue
        if _outside_content(anchor) or anchor.find_parent(_outside_content):
            continue
        label = anchor.get_text(" ", strip=True) or anchor.get("aria-label", "") or anchor.get("title", "")
        found[url] = " ".join(label.split()) or unquote(parts.path.rsplit("/", 1)[-1])
    return [(label, url) for url, label in found.items()]


def with_documents(text: str, documents: list[tuple[str, str]]) -> str:
    # Trafilatura drops a list that holds nothing but links, as navigation. On a worksheet page
    # that list is the material, so the files the content links to and the text lacks follow it.
    missing = []
    for label, url in documents:
        if url not in text:
            label = label.replace("[", r"\[").replace("]", r"\]")
            target = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            missing.append(f"- [{label}]({target})")
    return text + "\n\n---\n\n" + "\n".join(missing) if missing else text


def _prepared(data: bytes, content_type: str | None, url: str | None, converter: str):
    """The document the converters see: any embedded payload unwrapped, non-content tags
    gone and every reference absolute - or gone, when no URL parser accepts it. Returns the
    converter to start from, which an embedded payload may change."""
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
    base = (absolute_url(url or "", base_tag["href"]) if base_tag else None) or url or ""
    for tag in soup.select("[href], [src]"):
        for attribute in ("href", "src"):
            if tag.get(attribute):
                target = absolute_url(base, tag[attribute])
                if target is None:
                    del tag[attribute]  # the link's text stays
                else:
                    tag[attribute] = target
    return soup, converter


def _attempt(candidate: str, html: str, url: str | None, heading: str, clean: bool) -> str | None:
    """One converter's turn. Whatever it raises is the caller's to record."""
    if candidate == "trafilatura":
        if not clean:
            return html2txt(html)
        return with_heading(
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
    if candidate == "markitdown":
        return markitdown_stream(html.encode(), "text/html; charset=utf-8", ".html", url)
    return BeautifulSoup(html, "lxml").get_text("\n", strip=True)


def convert_html(
    data: bytes, content_type: str | None, url: str | None, converter: str, clean: bool
) -> ConversionResult:
    soup, converter = _prepared(data, content_type, url, converter)
    if not soup.get_text(" ", strip=True):
        return ConversionResult()
    html = str(soup)
    heading = page_heading(soup)
    warnings = []
    candidates = [converter] + (["markitdown", "bs4"] if converter == "trafilatura" else ["bs4"])
    for candidate in dict.fromkeys(candidates):
        try:
            text = _attempt(candidate, html, url, heading, clean)
        except Exception as exc:
            warnings.append(f"{candidate} failed ({type(exc).__name__}); trying fallback")
            continue
        if text and text.strip():
            text = text.strip()
            if candidate == "trafilatura" and clean:
                text = with_documents(text, document_links(soup))
            return ConversionResult(enhance_table_structure(text), candidate, "ok", warnings)
    return ConversionResult(status="failed", warnings=warnings or ["No converter produced text"])
