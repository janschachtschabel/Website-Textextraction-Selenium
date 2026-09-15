"""Browser routing uses the extracted result, never a truncated preview."""

import re

from bs4 import BeautifulSoup

from .markup import decode_text
from .results import ConversionResult, FetchResult

_CHALLENGE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(just a moment|checking your browser|verifying you are human|attention required)", re.I
)


def blocked_content(text: str, status: int | None) -> bool:
    return bool((status is not None and status >= 400) or (len(text) < 1000 and _CHALLENGE.search(text)))


def needs_browser(fetched: FetchResult, converted: ConversionResult) -> bool:
    if fetched.truncated or fetched.status_code is None or not 200 <= fetched.status_code < 300:
        return False
    if "html" not in (fetched.content_type or "").lower():
        return False
    if blocked_content(converted.markdown, fetched.status_code):
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
