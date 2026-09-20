"""Browser routing uses the extracted result, never a truncated preview."""

import re

from bs4 import BeautifulSoup

from .markup import decode_text
from .results import ConversionResult, FetchResult

_CHALLENGE = re.compile(
    r"\s*(?:#{1,6}\s*)?(just a moment|checking your browser|verifying you are human|attention required)", re.I
)


def blocked_content(text: str, status: int | None) -> bool:
    if status is not None and status >= 400:
        return True
    if len(text) >= 1000:
        return False
    # The phrase opens the first or second non-empty line; the first can be the site name or a heading.
    lines = [line for line in text.splitlines() if line.strip()]
    return any(_CHALLENGE.match(line) for line in lines[:2])


_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
SHELL_TEXT_LIMIT = 1000  # both shell rules need a page with almost no text


def visible_length(markdown: str) -> int:
    """Characters a reader sees: image alt text and link targets are not on the page."""
    text = _LINK_TARGET.sub("]", _IMAGE.sub("", markdown))
    return len(re.sub(r"\W+", "", text))


def needs_browser(fetched: FetchResult, converted: ConversionResult) -> bool:
    if fetched.truncated or fetched.status_code is None or not 200 <= fetched.status_code < 300:
        return False
    if "html" not in (fetched.content_type or "").lower():
        return False
    if blocked_content(converted.markdown, fetched.status_code):
        return False
    # This much extracted text answers both rules below, without parsing the document again.
    if converted.status == "ok" and visible_length(converted.markdown) >= SHELL_TEXT_LIMIT:
        return False
    soup = BeautifulSoup(decode_text(fetched.data, fetched.content_type), "lxml")
    has_script = bool(soup.find("script", src=True))
    has_root = bool(soup.select_one("#root, #app, #__next, [ng-version]"))
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible = soup.get_text(" ", strip=True)
    requires_js = bool(
        re.search(r"(enable javascript|javascript (?:required|wird benötigt)|javascript.*aktivieren)", visible, re.I)
    )
    shell_text = visible.lower().strip(" .…!") in {"", "loading", "loading content", "wird geladen"}
    return ((converted.status != "ok" or shell_text) and has_script and has_root) or (
        requires_js and len(visible) < 500
    )
