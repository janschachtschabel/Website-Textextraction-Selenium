"""Browser routing uses the extracted result, never a truncated preview."""

import re

from bs4 import BeautifulSoup

from .embedded_content import embedded_html
from .markup import decode_text
from .results import ConversionResult, FetchResult

# Checks a browser can pass run a script and reload into the page. Cloudflare's "Attention
# Required!" block page never does; it only counts as blocked.
_PASSABLE = re.compile(r"\s*(?:#{1,6}\s*)?(just a moment|checking your browser|verifying you are human)", re.I)
_BLOCKED = re.compile(
    r"\s*(?:#{1,6}\s*)?(just a moment|checking your browser|verifying you are human|attention required)", re.I
)


def _opens_with(phrase: re.Pattern, text: str) -> bool:
    if len(text) >= 1000:
        return False
    # The phrase opens the first or second non-empty line; the first can be the site name or a heading.
    lines = [line for line in text.splitlines() if line.strip()]
    return any(phrase.match(line) for line in lines[:2])


def challenge_text(text: str) -> bool:
    """A bot check such as Cloudflare's "Just a moment...", which a browser may pass."""
    return _opens_with(_PASSABLE, text)


def blocked_content(text: str, status: int | None) -> bool:
    return (status is not None and status >= 400) or _opens_with(_BLOCKED, text)


_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
# Below this, the plain HTML did not give the page. On 18 real pages measured on 2026-09-23,
# every page that rendering rescued extracted at most 297 visible characters over HTTP,
# while rendering made LearningApps (571) and a KMap lesson (761) worse.
THIN_TEXT_LIMIT = 500


def visible_length(markdown: str) -> int:
    """Characters a reader sees: image alt text and link targets are not on the page."""
    text = _LINK_TARGET.sub("]", _IMAGE.sub("", markdown))
    return len(re.sub(r"\W+", "", text))


def _runs_javascript(soup: BeautifulSoup) -> bool:
    """A script the browser executes; JSON-LD and other data blocks run nothing."""
    for script in soup.find_all("script"):
        kind = (script.get("type") or "").strip().lower()
        if kind in {"", "module"} or "javascript" in kind or "ecmascript" in kind:
            return True
    return False


def needs_browser(fetched: FetchResult, converted: ConversionResult) -> bool:
    """Whether auto mode renders: the plain HTML gave too little text, and the page runs the
    JavaScript that may add it. A bot challenge qualifies whatever its status, since a browser
    may pass it; any other error answer would render the same."""
    if fetched.truncated or fetched.status_code is None:
        return False
    if "html" not in (fetched.content_type or "").lower():
        return False
    if not (200 <= fetched.status_code < 300 or challenge_text(converted.markdown)):
        return False
    # This much extracted text answers the question without parsing the document again.
    if converted.status == "ok" and visible_length(converted.markdown) >= THIN_TEXT_LIMIT:
        return False
    soup = BeautifulSoup(decode_text(fetched.data, fetched.content_type), "lxml")
    # A page that carries its content as data - a KMap lesson, a YouTube video - shows a browser
    # no more of it; YouTube shows a fresh browser its consent page instead.
    if embedded_html(soup, fetched.final_url):
        return False
    return _runs_javascript(soup)
